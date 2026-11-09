"""在会话数据库内保存运行检查点、执行意图及可重放事件。"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import store

SCHEMA_VERSION = 1
# 每 5 秒由独立心跳续租 30 秒；崩溃后最多等待 30 秒即可安全接管，防止两个进程同时恢复。
LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 5
PROCESS_OWNER = uuid.uuid4().hex
TERMINAL = {"completed", "cancelled", "failed", "expired"}


def expire_checkpoints(max_age_seconds: int) -> None:
    """过期未活动的检查点释放附件保护，活跃租约不受影响。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat(timespec="seconds")
    with store._lock:
        conn = store._connect()
        conn.execute("UPDATE agent_checkpoints SET status='expired' WHERE updated_at<? AND lease_until<? "
                     "AND status NOT IN ('completed','cancelled','failed','expired')", (cutoff, time.time()))
        conn.commit()


def load_checkpoint(run_id: str) -> dict[str, Any] | None:
    """读取检查点和事件，不隐式启动任务。

    Args:
        run_id: 运行标识。

    Returns:
        含版本、配置和状态的检查点，不存在时返回空。
    """
    with store._lock:
        conn = store._connect()
        row = conn.execute("SELECT * FROM agent_checkpoints WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        result["state"] = json.loads(result.pop("state_json"))
        result["approval"] = json.loads(result.pop("approval_json") or "null")
        result["events"] = [item[0] for item in conn.execute(
            "SELECT payload FROM agent_checkpoint_events WHERE run_id=? ORDER BY seq", (run_id,))]
        result["checkpoint_seq"] = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM agent_checkpoint_steps WHERE run_id=?", (run_id,)).fetchone()[0]
        result["resumable"] = (result["schema_version"] == SCHEMA_VERSION and result["status"] not in TERMINAL
                               and (result["status"] == "paused" or result["lease_until"] < time.time()))
        return result


def list_checkpoints(session_id: str | None = None) -> list[dict[str, Any]]:
    """枚举未结束运行，支持按当前会话筛选。"""
    with store._lock:
        query = "SELECT run_id FROM agent_checkpoints WHERE status NOT IN ('completed','cancelled','failed','expired')"
        args: tuple = ()
        if session_id:
            query += " AND session_id=?"
            args = (session_id,)
        ids = [row[0] for row in store._connect().execute(query + " ORDER BY updated_at DESC", args)]
    records = []
    for run_id in ids:
        record = load_checkpoint(run_id)
        if record is None:
            continue
        session = store.get_session(record["session_id"]) if record["resumable"] else None
        if session and any(item.get("role") == "assistant" and item.get("metadata", {}).get("agent_run_id") == run_id
                           for item in session.get("messages", [])):
            # 最终消息已提交而终态事件未落盘的窗口：不再要求用户恢复，也不重复生成交付。
            SqliteCheckpointStore(run_id, record["owner"]).finish("completed")
            continue
        records.append(record)
    return records


class SqliteCheckpointStore:
    """带所有权校验的单次运行存储适配器。"""

    def __init__(self, run_id: str, owner: str = PROCESS_OWNER) -> None:
        self.run_id = run_id
        self.owner = owner

    def create(self, session_id: str, config: dict, state: dict, trace_id: str) -> None:
        """保存不含客户端和凭据的初始状态与恢复配置。"""
        with store._lock:
            conn = store._connect()
            conn.execute("INSERT INTO agent_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                self.run_id, session_id, SCHEMA_VERSION, "running", json.dumps(config, ensure_ascii=False),
                json.dumps(state, ensure_ascii=False), trace_id, self.owner,
                time.time() + LEASE_SECONDS, None, store.now_iso(),
            ))
            conn.commit()

    def claim(self) -> None:
        """原子接管暂停或租约失效的运行；并发恢复只有一个成功。"""
        with store._lock:
            conn = store._connect()
            changed = conn.execute(
                "UPDATE agent_checkpoints SET owner=?,status='running',lease_until=?,updated_at=? "
                "WHERE run_id=? AND schema_version=? AND status NOT IN ('completed','cancelled','failed','expired') "
                "AND (status='paused' OR lease_until<?)",
                (self.owner, time.time()+LEASE_SECONDS, store.now_iso(), self.run_id, SCHEMA_VERSION, time.time()),
            ).rowcount
            conn.commit()
            if changed != 1:
                raise ValueError("任务仍在运行、已结束或检查点版本不兼容，无法接管")

    def _owned(self, conn: Any) -> None:
        """每次真实操作前确认租约所有者，已接管的旧进程不能继续写入。"""
        changed = conn.execute("UPDATE agent_checkpoints SET lease_until=?,updated_at=? "
            "WHERE run_id=? AND owner=? AND status='running'",
            (time.time()+LEASE_SECONDS, store.now_iso(), self.run_id, self.owner)).rowcount
        if changed != 1:
            conn.rollback()
            raise RuntimeError("运行检查点租约已失效")

    def heartbeat(self) -> None:
        """在模型或审批长时间等待期间独立续租。"""
        with store._lock:
            conn = store._connect()
            self._owned(conn)
            conn.commit()

    def set_state(self, state: dict) -> None:
        """只在尚无决策日志时补充附件上下文基线。"""
        with store._lock:
            conn = store._connect()
            self._owned(conn)
            conn.execute("UPDATE agent_checkpoints SET state_json=? WHERE run_id=? AND NOT EXISTS "
                "(SELECT 1 FROM agent_checkpoint_steps WHERE run_id=?)",
                (json.dumps(state, ensure_ascii=False), self.run_id, self.run_id))
            conn.commit()

    def steps(self) -> list[dict[str, Any]]:
        """按执行顺序读取已完成结果与未决意图。"""
        with store._lock:
            rows = store._connect().execute("SELECT * FROM agent_checkpoint_steps WHERE run_id=? ORDER BY seq", (self.run_id,)).fetchall()
            return [{"seq": row["seq"], "kind": row["kind"], "arguments": json.loads(row["arguments_json"]),
                     "completed": bool(row["completed"]), "payload": json.loads(row["payload_json"] or "null")} for row in rows]

    def begin(self, kind: str, arguments: Any) -> int:
        """工具产生副作用之前提交执行意图。"""
        with store._lock:
            conn = store._connect()
            self._owned(conn)
            sequence = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM agent_checkpoint_steps WHERE run_id=?", (self.run_id,)).fetchone()[0]
            conn.execute("INSERT INTO agent_checkpoint_steps(run_id,seq,kind,arguments_json) VALUES (?,?,?,?)",
                         (self.run_id, sequence, kind, json.dumps(arguments, ensure_ascii=False)))
            conn.commit()
            return sequence

    def complete(self, sequence: int, payload: Any) -> None:
        """结果与完成标记原子保存，重演时不会再次执行工具。"""
        with store._lock:
            conn = store._connect()
            self._owned(conn)
            conn.execute("UPDATE agent_checkpoint_steps SET payload_json=?,completed=1 WHERE run_id=? AND seq=?",
                         (json.dumps(payload, ensure_ascii=False), self.run_id, sequence))
            conn.commit()

    def event(self, line: str) -> None:
        """在公开事件交给浏览器之前持久化。"""
        with store._lock:
            conn = store._connect()
            self._owned(conn)
            conn.execute("INSERT INTO agent_checkpoint_events SELECT ?,COALESCE(MAX(seq),0)+1,? "
                         "FROM agent_checkpoint_events WHERE run_id=?", (self.run_id, line, self.run_id))
            conn.commit()

    def approval(self, payload: dict | None) -> None:
        """先落盘待审批请求或已接受的决定，再允许执行继续。"""
        with store._lock:
            conn = store._connect()
            self._owned(conn)
            conn.execute("UPDATE agent_checkpoints SET approval_json=? WHERE run_id=?",
                         (json.dumps(payload, ensure_ascii=False), self.run_id))
            conn.commit()

    def finish(self, status: str) -> None:
        """保存终态或暂停态，不删除仍可恢复的附件。"""
        if status not in TERMINAL | {"paused"}:
            raise ValueError("无效的运行终态")
        with store._lock:
            conn = store._connect()
            conn.execute("UPDATE agent_checkpoints SET status=?,lease_until=0,updated_at=? WHERE run_id=? AND owner=?",
                         (status, store.now_iso(), self.run_id, self.owner))
            conn.commit()
