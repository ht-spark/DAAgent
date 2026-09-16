"""读取当前会话已授权的文本附件。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

TEXT_ATTACHMENT_SUFFIXES = frozenset(
    {".csv", ".json", ".jsonl", ".markdown", ".md", ".ndjson", ".py"}
)
MAX_ATTACHMENT_READ_CHARS = 80_000


def read_attachment_file(
    arguments: Mapping[str, Any], context: Mapping[str, Any]
) -> dict[str, Any]:
    """按文件名分段读取本轮已授权的 UTF-8 文本附件，不执行内容。"""
    if set(arguments) - {"file_name", "offset", "limit"} or "file_name" not in arguments:
        raise ValueError("read_attachment_file 需要 file_name，可选 offset 和 limit")
    file_name = arguments["file_name"]
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", 300)
    if not isinstance(file_name, str) or not file_name or Path(file_name).name != file_name:
        raise ValueError("file_name 必须是已上传附件的文件名")
    if type(offset) is not int or type(limit) is not int or offset < 1 or not 1 <= limit <= 500:
        raise ValueError("offset 必须大于 0，limit 必须在 1 到 500 之间")
    authorized = context.get("authorized_paths")
    if not isinstance(authorized, list):
        raise PermissionError("缺少附件授权上下文")
    matches = [Path(path) for path in authorized if isinstance(path, str) and Path(path).name.casefold() == file_name.casefold()]
    if len(matches) != 1:
        raise PermissionError("附件未授权或文件名不唯一")
    target = matches[0]
    if target.suffix.lower() not in TEXT_ATTACHMENT_SUFFIXES or not target.is_file():
        raise ValueError("附件不是可读取的文本文件")
    chunks: list[str] = []
    used_chars = 0
    end_line = offset - 1
    truncated = False
    with target.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, start=1):
            if line_number < offset:
                continue
            if line_number >= offset + limit:
                truncated = True
                break
            remaining = MAX_ATTACHMENT_READ_CHARS - used_chars
            if len(line) > remaining:
                chunks.append(line[:remaining])
                end_line = line_number
                truncated = True
                break
            chunks.append(line)
            used_chars += len(line)
            end_line = line_number
    return {
        "file_name": target.name,
        "content": "".join(chunks),
        "start_line": offset,
        "end_line": end_line,
        "truncated": truncated,
    }
