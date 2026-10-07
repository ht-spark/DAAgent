"""读取当前会话已授权的文本附件。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .project_edit_tools import (
    MAX_COMMAND_OUTPUT_CHARS,
    MAX_FILE_EDITS,
    MAX_TEXT_BYTES,
    coerce_file_edits,
    edit_workspace_file,
)
from .workspace_tools import resolve_workspace_member_path

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
    """把上传附件映射到项目原文件后执行 Pi 风格的原位编辑。

    Args:
        arguments: 附件授权路径、可选项目目标路径与一组精确替换。
        context: 本轮授权附件路径和已绑定项目目录。

    Returns:
        被原位修改的项目文件信息和实际 diff。
    """
    if (
        set(arguments) - {"file_path", "target_path", "edits"}
        or "file_path" not in arguments
        or "edits" not in arguments
    ):
        raise ValueError("edit_attachment_file 参数无效")
    try:
        edits = coerce_file_edits(arguments.get("edits"))
    except ValueError as exc:
        raise ValueError("edit_attachment_file 参数无效") from exc
    if len(edits) > MAX_FILE_EDITS:
        raise ValueError("edit_attachment_file 参数无效")
    source = _authorized_attachment_path(arguments.get("file_path"), context)
    if source.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("目标附件超过可编辑大小限制")
    workspace_value = context.get("workspace_root")
    if not isinstance(workspace_value, str) or not workspace_value.strip():
        raise ValueError(
            "浏览器上传不包含原文件路径；请先绑定原文件所在项目，无法原位修改时不会生成副本"
        )
    workspace_root = Path(workspace_value).expanduser().resolve(strict=True)
    requested_target = arguments.get("target_path", source.name)
    if not isinstance(requested_target, str) or not requested_target.strip():
        raise ValueError("target_path 必须是当前项目中的有效文件路径")
    target = resolve_workspace_member_path(workspace_root, requested_target)
    if not target.is_file():
        raise ValueError(
            f"当前项目中未找到附件原文件: {requested_target}；不会在 outputs 中生成副本"
        )
    relative_path = target.relative_to(workspace_root).as_posix()
    result = edit_workspace_file(
        workspace_root,
        relative_path,
        edits,
        expected_content=source.read_bytes(),
    )
    result.update({
        "absolute_path": str(target),
        "name": target.name,
        "format": target.suffix.lower().removeprefix("."),
        "size_bytes": target.stat().st_size,
        "in_place": True,
        "diff": str(result.get("diff", ""))[:MAX_COMMAND_OUTPUT_CHARS],
        "patch": str(result.get("patch", ""))[:MAX_COMMAND_OUTPUT_CHARS],
        "description": f"已原位修改项目文件 {relative_path}，共应用 {len(edits)} 项精确修改",
    })
    return result
