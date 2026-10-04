"""读取当前会话已授权的文本附件。"""

from __future__ import annotations

import difflib
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from .artifact_tools import ArtifactWorkspace
from .project_edit_tools import (
    FileEdit,
    MAX_COMMAND_OUTPUT_CHARS,
    MAX_FILE_EDITS,
    MAX_TEXT_BYTES,
    prepare_exact_text_edits,
)

TEXT_ATTACHMENT_SUFFIXES = frozenset(
    {".csv", ".json", ".jsonl", ".markdown", ".md", ".ndjson", ".py", ".txt"}
)
MAX_ATTACHMENT_READ_CHARS = 200_000


def _authorized_attachment_path(
    file_path: Any,
    context: Mapping[str, Any],
) -> Path:
    """解析并验证本轮获授权的文本附件路径。"""
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path 必须是 available_data_files 中的有效路径")
    authorized = context.get("authorized_paths")
    if not isinstance(authorized, list):
        raise PermissionError("缺少附件授权上下文")
    target = Path(file_path).expanduser().resolve(strict=False)
    authorized_paths = {
        Path(path).expanduser().resolve(strict=False)
        for path in authorized
        if isinstance(path, str)
    }
    if target not in authorized_paths:
        raise PermissionError("附件路径未获当前 Runtime 授权")
    if target.suffix.lower() not in TEXT_ATTACHMENT_SUFFIXES or not target.is_file():
        raise ValueError("附件不是可读取的文本文件")
    return target


def read_attachment_file(
    arguments: Mapping[str, Any], context: Mapping[str, Any]
) -> dict[str, Any]:
    """按 Runtime 路径分段读取本轮已授权的 UTF-8 文本附件，不执行内容。"""
    if set(arguments) - {"file_path", "offset", "limit"} or "file_path" not in arguments:
        raise ValueError("read_attachment_file 需要 file_path，可选 offset 和 limit")
    file_path = arguments["file_path"]
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", 300)
    if type(offset) is not int or type(limit) is not int or offset < 1 or not 1 <= limit <= 500:
        raise ValueError("offset 必须大于 0，limit 必须在 1 到 500 之间")
    target = _authorized_attachment_path(file_path, context)
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
        "file_path": str(target),
        "content": "".join(chunks),
        "start_line": offset,
        "end_line": end_line,
        "truncated": truncated,
    }


def edit_attachment_file(
    arguments: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """精确编辑上传文本附件，并以原文件名写出一个更新后的会话产物。

    Args:
        arguments: 附件授权路径与一组 Pi 风格精确替换。
        context: 本轮授权附件路径和会话产物目录。

    Returns:
        更新副本的产物信息和实际 diff。
    """
    if set(arguments) != {"file_path", "edits"}:
        raise ValueError("edit_attachment_file 参数无效")
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits or len(edits) > MAX_FILE_EDITS:
        raise ValueError("edit_attachment_file 参数无效")
    if not all(
        isinstance(edit, dict)
        and set(edit) == {"old_text", "new_text"}
        and isinstance(edit["old_text"], str)
        and isinstance(edit["new_text"], str)
        for edit in edits
    ):
        raise ValueError("edit_attachment_file 参数无效")
    source = _authorized_attachment_path(arguments.get("file_path"), context)
    if source.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("目标附件超过可编辑大小限制")
    encoded, original, updated = prepare_exact_text_edits(
        source.read_bytes(),
        cast(list[FileEdit], edits),
    )
    if len(encoded) > MAX_TEXT_BYTES:
        raise ValueError("修改后的附件超过大小限制")

    output_dir = context.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ValueError("edit_attachment_file 缺少有效的产物目录上下文")
    workspace = ArtifactWorkspace(output_dir)
    target = workspace.path(source.name, overwrite=True)
    existed = target.exists()
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{source.name}",
        tofile=f"b/{source.name}",
    ))
    return {
        "path": source.name,
        "name": source.name,
        "format": source.suffix.lower().removeprefix("."),
        "size_bytes": target.stat().st_size,
        "overwritten": existed,
        "edit_count": len(edits),
        "diff": diff[:MAX_COMMAND_OUTPUT_CHARS],
        "description": f"已按原文件名更新上传附件，共应用 {len(edits)} 项精确修改",
    }
