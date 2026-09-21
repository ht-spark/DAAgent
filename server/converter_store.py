"""文档转换历史存储：SQLite 单库（conversions 表）。

每条记录保存一次转换的元数据与 Markdown 正文。正文与元数据同库，
避免「索引文件 + 正文文件」两处状态不一致；未删除的记录会在每次打开
「文档转换」页面时重新列出。

与 server/store.py 保持同一套轻量约定：模块级连接 + 线程锁。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).resolve().parent / "converter.db"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# 历史上限：超出后按时间丢弃最早的记录，避免本地库无限增长
MAX_HISTORY_RECORDS = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT    NOT NULL,
    ok           INTEGER NOT NULL,
    char_count   INTEGER NOT NULL DEFAULT 0,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    elapsed_ms   INTEGER NOT NULL DEFAULT 0,
    converted_at TEXT    NOT NULL,
    error        TEXT,
    markdown     TEXT
);
"""


def _get_conn() -> sqlite3.Connection:
    """返回模块级共享连接，首次调用时建表。"""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def _now_iso() -> str:
    """本地时区的 ISO 时间戳（秒精度）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _to_record(row: sqlite3.Row, *, with_markdown: bool = False) -> dict[str, Any]:
    """把数据库行转成接口用的记录字典。"""
    record: dict[str, Any] = {
        "id": int(row["id"]),
        "filename": row["filename"],
        "ok": bool(row["ok"]),
        "char_count": int(row["char_count"]),
        "size_bytes": int(row["size_bytes"]),
        "elapsed_ms": int(row["elapsed_ms"]),
        "converted_at": row["converted_at"],
        "error": row["error"],
    }
    if with_markdown:
        record["markdown"] = row["markdown"] or ""
    return record


def add_record(
    *,
    filename: str,
    ok: bool,
    char_count: int = 0,
    size_bytes: int = 0,
    elapsed_ms: int = 0,
    error: str | None = None,
    markdown: str | None = None,
) -> dict[str, Any]:
    """写入一条转换记录并返回其元数据。

    Args:
        filename: 原始文件名。
        ok: 是否转换成功（失败也记录，便于在列表里看到原因）。
        char_count: 生成的 Markdown 字符数。
        size_bytes: 原始文件字节数。
        elapsed_ms: 转换耗时（毫秒）。
        error: 失败原因，成功时为 None。
        markdown: Markdown 正文，失败时为 None。

    Returns:
        含 ``id`` 与转换时间的记录字典（不含正文）。
    """
    converted_at = _now_iso()
    with _lock:
        conn = _get_conn()
        cursor = conn.execute(
            """
            INSERT INTO conversions
                (filename, ok, char_count, size_bytes, elapsed_ms, converted_at, error, markdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                filename,
                int(ok),
                int(char_count),
                int(size_bytes),
                int(elapsed_ms),
                converted_at,
                error,
                markdown,
            ),
        )
        record_id = int(cursor.lastrowid or 0)
        # 超出上限时丢弃最早的记录
        conn.execute(
            """
            DELETE FROM conversions
            WHERE id NOT IN (
                SELECT id FROM conversions ORDER BY id DESC LIMIT ?
            )
            """,
            (MAX_HISTORY_RECORDS,),
        )
        conn.commit()

    return {
        "id": record_id,
        "filename": filename,
        "ok": ok,
        "char_count": int(char_count),
        "size_bytes": int(size_bytes),
        "elapsed_ms": int(elapsed_ms),
        "converted_at": converted_at,
        "error": error,
    }


def list_records() -> list[dict[str, Any]]:
    """按转换先后顺序返回全部记录（不含正文）。"""
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM conversions ORDER BY id ASC"
        ).fetchall()
    return [_to_record(row) for row in rows]


def get_record(record_id: int) -> dict[str, Any] | None:
    """按 ID 返回单条记录的元数据。"""
    with _lock:
        row = _get_conn().execute(
            "SELECT * FROM conversions WHERE id = ?", (int(record_id),)
        ).fetchone()
    return _to_record(row) if row is not None else None


def read_markdown(record_id: int) -> str | None:
    """读取指定记录的 Markdown 正文；记录不存在时返回 None。"""
    with _lock:
        row = _get_conn().execute(
            "SELECT markdown FROM conversions WHERE id = ?", (int(record_id),)
        ).fetchone()
    if row is None:
        return None
    return row["markdown"] or ""


def delete_record(record_id: int) -> bool:
    """删除指定记录，返回是否确实删掉了一条。"""
    with _lock:
        conn = _get_conn()
        cursor = conn.execute("DELETE FROM conversions WHERE id = ?", (int(record_id),))
        conn.commit()
    return cursor.rowcount > 0
