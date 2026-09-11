"""一次性迁移：discussion_history.json + runs.db → server/agent.db。

用法：
    python -m server.migrate_unified_store

幂等：已存在的 session/message id 会跳过。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import store  # noqa: E402

DISCUSSIONS_PATH = PROJECT_ROOT / "server" / "discussion_history.json"
RUNS_DB_PATH = PROJECT_ROOT / "server" / "runs.db"
_RUN_KIND = "modeling"


def _session_exists(session_id: str) -> bool:
    return store.get_session(session_id) is not None


def _message_exists(message_id: str) -> bool:
    return store.get_message(message_id) is not None


def migrate_discussions() -> int:
    if not DISCUSSIONS_PATH.exists():
        print(f"[skip] 无讨论文件：{DISCUSSIONS_PATH}")
        return 0
    try:
        data = json.loads(DISCUSSIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[error] 读取讨论文件失败：{exc}")
        return 0
    if not isinstance(data, list):
        print("[error] 讨论文件格式异常，期望 JSON 数组")
        return 0

    count = 0
    for item in data:
        sid = str(item.get("id") or "").strip()
        if not sid or _session_exists(sid):
            continue
        title = (item.get("title") or "未命名会话").strip()[:80]
        store.create_session(title, session_id=sid)
        for msg in item.get("messages") or []:
            role = msg.get("role") or store.ROLE_USER
            if role not in (store.ROLE_USER, store.ROLE_ASSISTANT):
                role = store.ROLE_USER
            meta: dict = {"sources": msg.get("sources") or []}
            if msg.get("attachments"):
                meta["attachments"] = msg["attachments"]
            store.append_message(
                sid,
                role,
                content=msg.get("content") or "",
                metadata=meta,
            )
        # 回写原始时间戳（best-effort）
        _force_session_times(sid, item.get("created_at"), item.get("updated_at"))
        count += 1
    print(f"[ok] 讨论迁移 {count} 条会话")
    return count


def _force_session_times(
    session_id: str, created_at: str | None, updated_at: str | None
) -> None:
    from server.store import _connect, _lock

    with _lock:
        conn = _connect()
        if created_at:
            conn.execute(
                "UPDATE sessions SET created_at=? WHERE id=?",
                (created_at, session_id),
            )
        if updated_at:
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (updated_at, session_id),
            )
        conn.commit()


def migrate_runs() -> int:
    if not RUNS_DB_PATH.exists():
        print(f"[skip] 无旧 runs 库：{RUNS_DB_PATH}")
        return 0
    conn = sqlite3.connect(str(RUNS_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM runs").fetchall()
    except sqlite3.Error as exc:
        print(f"[error] 读取 runs.db 失败：{exc}")
        conn.close()
        return 0

    count = 0
    for row in rows:
        run_id = row["run_id"]
        if not run_id or _message_exists(run_id):
            continue
        try:
            progress = json.loads(row["progress_json"] or "[]")
        except json.JSONDecodeError:
            progress = []
        try:
            artifacts = json.loads(row["artifacts_json"] or "[]")
        except json.JSONDecodeError:
            artifacts = []
        try:
            model_config = json.loads(row["model_config"] or "{}")
        except json.JSONDecodeError:
            model_config = {}

        title = (row["task_name"] or row["problem_preview"] or "建模任务")[:80]
        session_id = f"run_{run_id}"
        if not _session_exists(session_id):
            store.create_session(title, session_id=session_id)
        store.append_message(
            session_id,
            store.ROLE_TOOL,
            content=row["problem_preview"] or title,
            message_id=run_id,
            metadata={
                "kind": _RUN_KIND,
                "status": row["status"] or "failed",
                "problem_preview": row["problem_preview"],
                "task_name": row["task_name"],
                "model_config": model_config,
                "workflow_status": row["workflow_status"],
                "current_question_id": row["current_question_id"],
                "results_count": row["results_count"],
                "paper_title": row["paper_title"],
                "review_status": row["review_status"],
                "error": row["error"],
                "progress": progress,
                "artifacts": artifacts,
                "migrated_from": "runs.db",
            },
        )
        _force_session_times(session_id, row["created_at"], row["updated_at"])
        count += 1
    conn.close()
    print(f"[ok] 建模任务迁移 {count} 条")
    return count


def main() -> None:
    # 先触发建库
    store._connect()
    n1 = migrate_discussions()
    n2 = migrate_runs()
    sessions = store.list_sessions(limit=1000)
    print(
        f"完成：讨论 {n1}，任务 {n2}；"
        f"当前库会话总数 {len(sessions)} → {store._DB_PATH}"
    )


if __name__ == "__main__":
    main()
