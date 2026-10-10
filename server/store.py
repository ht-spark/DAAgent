"""统一会话存储：SQLite 单库（sessions + messages）。

建模运行不再使用独立 runs 表：一条 tool 消息（id=run_id）承载
状态、进度与产物元数据；大文件仍落盘 artifacts/。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).resolve().parent / "agent.db"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                root_path TEXT NOT NULL,
                root_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                project_id TEXT,
                cwd TEXT,
                model_config_id TEXT,
                reasoning_effort TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, seq),
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, seq);
            """
        )
        session_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "project_id" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT")
        if "cwd" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
        if "model_config_id" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN model_config_id TEXT")
        if "reasoning_effort" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN reasoning_effort TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_project "
            "ON sessions(project_id, updated_at)"
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        _conn = conn
    return _conn


def _meta(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _session_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE id=?", (session_id,)
    ).fetchone()


def create_project(title: str, root_path: str, root_key: str) -> str:
    """创建项目注册记录并返回项目标识。"""
    project_id = uuid.uuid4().hex
    normalized_title = (title or "未命名项目").strip()[:80] or "未命名项目"
    ts = now_iso()
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO projects
                (id, title, root_path, root_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, normalized_title, root_path, root_key, ts, ts),
        )
        conn.commit()
    return project_id


def get_project(project_id: str) -> dict[str, Any] | None:
    """按标识读取项目注册记录。"""
    with _lock:
        row = _connect().execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
    return _project_dict(row) if row else None


def get_project_by_root_key(root_key: str) -> dict[str, Any] | None:
    """按规范路径键读取项目。"""
    with _lock:
        row = _connect().execute(
            "SELECT * FROM projects WHERE root_key=?", (root_key,)
        ).fetchone()
    return _project_dict(row) if row else None


def list_projects() -> list[dict[str, Any]]:
    """按最近更新时间列出已注册项目。"""
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM projects ORDER BY updated_at DESC"
        ).fetchall()
    return [_project_dict(row) for row in rows]


def rename_project(project_id: str, title: str) -> bool:
    """修改项目显示名称，不改变项目目录。"""
    normalized = (title or "").strip()[:80]
    if not normalized:
        return False
    with _lock:
        conn = _connect()
        cursor = conn.execute(
            "UPDATE projects SET title=?, updated_at=? WHERE id=?",
            (normalized, now_iso(), project_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_project(project_id: str) -> bool:
    """取消项目注册并撤销会话目录绑定，不删除真实文件。"""
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE sessions SET project_id=NULL, cwd=NULL WHERE project_id=?",
            (project_id,),
        )
        cursor = conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.commit()
        return cursor.rowcount > 0


def bind_session_project(session_id: str, project_id: str, cwd: str) -> bool:
    """把尚未绑定的会话固定到一个项目目录。"""
    with _lock:
        conn = _connect()
        row = _session_row(conn, session_id)
        if row is None:
            return False
        if row["project_id"] is not None:
            return row["project_id"] == project_id and row["cwd"] == cwd
        conn.execute(
            "UPDATE sessions SET project_id=?, cwd=?, updated_at=? WHERE id=?",
            (project_id, cwd, now_iso(), session_id),
        )
        conn.commit()
        return True


def create_session(
    title: str,
    session_id: str | None = None,
    *,
    project_id: str | None = None,
    cwd: str | None = None,
    model_config_id: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """创建会话，可选地固定到一个已注册项目。"""
    sid = session_id or uuid.uuid4().hex
    title = (title or "未命名会话").strip()[:80] or "未命名会话"
    ts = now_iso()
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO sessions
                (id, title, project_id, cwd, model_config_id, reasoning_effort, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, title, project_id, cwd, model_config_id, reasoning_effort, ts, ts),
        )
        conn.commit()
    return sid


def get_session(session_id: str) -> dict[str, Any] | None:
    with _lock:
        row = _session_row(_connect(), session_id)
        if row is None:
            return None
        messages = _connect().execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
    return {
        "id": row["id"],
        "title": row["title"],
        "project_id": row["project_id"],
        "cwd": row["cwd"],
        "model_config_id": row["model_config_id"],
        "reasoning_effort": row["reasoning_effort"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "messages": [_message_dict(m) for m in messages],
    }


def list_sessions(
    limit: int = 100,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """列出会话；提供项目标识时仅返回该项目的会话。"""
    with _lock:
        conn = _connect()
        if project_id is None:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE project_id=?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "project_id": row["project_id"],
            "cwd": row["cwd"],
            "model_config_id": row["model_config_id"],
            "reasoning_effort": row["reasoning_effort"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def rename_session(session_id: str, title: str) -> bool:
    normalized = (title or "").strip()[:80]
    if not normalized:
        return False
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
            (normalized, now_iso(), session_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_session_model_config(
    session_id: str,
    config_id: str,
    reasoning_effort: str | None = None,
) -> bool:
    """保存当前会话固定使用的 API 模型配置和思考程度。"""
    normalized = config_id.strip()[:200]
    if not normalized:
        return False
    with _lock:
        conn = _connect()
        cursor = conn.execute(
            "UPDATE sessions SET model_config_id=?, reasoning_effort=?, updated_at=? WHERE id=?",
            (normalized, reasoning_effort, now_iso(), session_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_session(session_id: str) -> bool:
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        cur2 = conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
        return cur2.rowcount > 0


def delete_message(message_id: str) -> bool:
    """删除单条消息；会话无剩余消息时一并删除会话。"""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT session_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if row is None:
            return False
        session_id = row["session_id"]
        conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if remaining is not None and int(remaining["c"]) == 0:
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
        return True


def _next_seq(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return int(row["m"]) + 1


def append_message(
    session_id: str,
    role: str,
    content: str = "",
    metadata: dict[str, Any] | None = None,
    message_id: str | None = None,
) -> str:
    """在会话末尾追加一条消息；会话不存在时自动创建。"""
    mid = message_id or uuid.uuid4().hex
    meta = metadata or {}
    ts = now_iso()
    with _lock:
        conn = _connect()
        if _session_row(conn, session_id) is None:
            title = content.strip()[:40] if content else "未命名会话"
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, (title or "未命名会话")[:80], ts, ts),
            )
        seq = _next_seq(conn, session_id)
        conn.execute(
            """
            INSERT INTO messages
                (id, session_id, seq, role, content, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mid,
                session_id,
                seq,
                role,
                content,
                json.dumps(meta, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        conn.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?",
            (ts, session_id),
        )
        conn.commit()
    return mid


def get_message(message_id: str) -> dict[str, Any] | None:
    with _lock:
        row = _connect().execute(
            "SELECT * FROM messages WHERE id=?", (message_id,)
        ).fetchone()
    return _message_dict(row) if row else None


def update_message_metadata(
    message_id: str,
    metadata: dict[str, Any] | None = None,
    content: str | None = None,
    merge: bool = True,
) -> bool:
    """更新工具消息的元数据/正文。merge=True 时浅合并 metadata。"""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if row is None:
            return False
        meta = _meta(row["metadata"])
        if metadata is not None:
            if merge:
                meta.update(metadata)
            else:
                meta = dict(metadata)
        new_content = row["content"] if content is None else content
        ts = now_iso()
        conn.execute(
            "UPDATE messages SET content=?, metadata=?, updated_at=? WHERE id=?",
            (new_content, json.dumps(meta, ensure_ascii=False), ts, message_id),
        )
        conn.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?",
            (ts, row["session_id"]),
        )
        conn.commit()
        return True


def list_tool_messages(kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """列出 tool 消息（默认全部 kind）。用于历史任务列表。"""
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM messages WHERE role=? ORDER BY updated_at DESC LIMIT ?",
            (ROLE_TOOL, limit),
        ).fetchall()
    items = [_message_dict(r) for r in rows]
    if kind:
        items = [m for m in items if (m.get("metadata") or {}).get("kind") == kind]
    return items


def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
    """把项目数据库记录转换为普通字典。"""
    return {
        "id": row["id"],
        "title": row["title"],
        "root_path": row["root_path"],
        "root_key": row["root_key"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "seq": row["seq"],
        "role": row["role"],
        "content": row["content"],
        "metadata": _meta(row["metadata"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def touch_session(session_id: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?",
            (now_iso(), session_id),
        )
        conn.commit()
