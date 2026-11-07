"""文档转换 Skill 复用的附件授权、转换缓存与分页实现。

用户上传的 PDF、Word、PPT、Excel、HTML、EPUB、Notebook 附件无法当作文本直接阅读，
这里复用确定性转换层（``server.document_tools``）先转成 Markdown 落盘，
再按行分页返回：既避免把整篇长文档一次性塞进模型上下文，也避免同一附件被反复转换。
转换本身不依赖 LLM，结果可复现。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from server.document_tools import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    to_markdown,
)

from .artifact_tools import ArtifactWorkspace
from .attachment_tools import TEXT_ATTACHMENT_SUFFIXES

__all__ = [
    "CONVERTIBLE_DOCUMENT_SUFFIXES",
    "DEFAULT_PAGE_LINES",
    "MAX_PAGE_CHARS",
    "MAX_PAGE_LINES",
    "convert_attachment_to_markdown",
]

# 需要转换才能阅读的附件类型：可转换格式去掉已被文本读取工具覆盖的部分
CONVERTIBLE_DOCUMENT_SUFFIXES = frozenset(SUPPORTED_DOCUMENT_EXTENSIONS) - TEXT_ATTACHMENT_SUFFIXES

# 分页参数：默认一次返回 400 行，单页字符数另有上限，防止单次结果过大
DEFAULT_PAGE_LINES = 400
MAX_PAGE_LINES = 1000
MAX_PAGE_CHARS = 60_000


def _converted_path(converted_dir: Path, source: Path) -> Path:
    """转换结果的落盘路径。

    文件名带上源路径的短哈希，避免不同目录下的同名附件相互覆盖。

    Args:
        converted_dir: 转换结果目录。
        source: 源附件路径（须为已解析的绝对路径）。

    Returns:
        形如 ``converted/题目.a1b2c3d4.md`` 的路径。
    """
    digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
    return converted_dir / f"{source.stem}.{digest}.md"


def _authorized_source(file_path: Any, context: Mapping[str, Any]) -> Path:
    """校验并返回已授权的附件绝对路径。

    Args:
        file_path: 模型给出的附件路径。
        context: 工具上下文，需包含 ``authorized_paths``。

    Returns:
        已解析的附件绝对路径。

    Raises:
        ValueError: 路径为空、不是文件，或扩展名不需要转换。
        PermissionError: 路径不在当前 Runtime 的授权范围内。
    """
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

    suffix = target.suffix.lower()
    if suffix in TEXT_ATTACHMENT_SUFFIXES:
        raise ValueError(f"{suffix} 属于纯文本附件，请直接使用 read_attachment_file 读取")
    if suffix not in CONVERTIBLE_DOCUMENT_SUFFIXES:
        raise ValueError(f"不支持转换 {suffix or '无扩展名'} 附件")
    if not target.is_file():
        raise ValueError(f"附件不存在或不是文件：{target.name}")
    return target


def _ensure_markdown(source: Path, converted_dir: Path) -> tuple[Path, bool]:
    """确保附件的 Markdown 转换结果已落盘，返回结果路径与本次是否发生转换。

    转换结果比源文件新时直接复用，避免同一附件被反复解析。

    Args:
        source: 源附件绝对路径。
        converted_dir: 转换结果目录。

    Returns:
        ``(markdown 路径, 本次是否新转换)``。

    Raises:
        DocumentConversionError: 转换失败（格式损坏、依赖缺失、内容为空等）。
    """
    workspace = ArtifactWorkspace(converted_dir)
    relative_path = _converted_path(workspace.root, source).relative_to(workspace.root)
    target = workspace.path(relative_path, overwrite=True)
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target, False

    markdown = to_markdown(source)
    target.write_text(markdown, encoding="utf-8")
    return target, True


def convert_attachment_to_markdown(
    arguments: Mapping[str, Any], context: Mapping[str, Any]
) -> dict[str, Any]:
    """把已授权的非 Markdown 附件转换为 Markdown，并按行窗口返回内容。

    首次调用会真正执行转换并把完整结果写入 ``markdown_path``，之后按同一路径读取时
    直接复用该结果；``truncated`` 为 true 时用返回的 ``next_offset`` 继续读取。

    Args:
        arguments: ``file_path``（必填）、``offset``（起始行，默认 1）、
            ``limit``（行数，默认 400，上限 1000）、``char_offset``（行内续读位置，默认 0）。
        context: 需包含 ``authorized_paths`` 与 ``converted_dir``。

    Returns:
        含文件信息、转换结果路径、分页内容与续读偏移的字典。

    Raises:
        ValueError: 参数非法或附件类型不支持转换。
        PermissionError: 附件未获授权。
        DocumentConversionError: 转换失败。
    """
    if set(arguments) - {"file_path", "offset", "limit", "char_offset"} or "file_path" not in arguments:
        raise ValueError("markitdown 需要 file_path，可选 offset、limit 和 char_offset")
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", DEFAULT_PAGE_LINES)
    char_offset = arguments.get("char_offset", 0)
    if type(offset) is not int or type(limit) is not int or offset < 1 or not 1 <= limit <= MAX_PAGE_LINES:
        raise ValueError(f"offset 必须大于 0，limit 必须在 1 到 {MAX_PAGE_LINES} 之间")
    if type(char_offset) is not int or char_offset < 0:
        raise ValueError("char_offset 必须是非负整数")

    source = _authorized_source(arguments["file_path"], context)
    converted_dir = context.get("converted_dir")
    if not isinstance(converted_dir, str) or not converted_dir.strip():
        raise ValueError("缺少转换结果目录上下文")

    markdown_path, converted_now = _ensure_markdown(source, Path(converted_dir))

    all_lines = markdown_path.read_text(encoding="utf-8").splitlines(keepends=True)
    total_lines = len(all_lines)
    total_chars = sum(len(line) for line in all_lines)

    window = all_lines[offset - 1 : offset - 1 + limit]
    if char_offset and (not window or char_offset > len(window[0])):
        raise ValueError("char_offset 超出起始行的字符范围")
    truncated = offset - 1 + len(window) < total_lines
    next_offset = offset + len(window) if truncated else None
    next_char_offset = 0
    page: list[str] = []
    used_chars = 0
    end_line = offset - 1
    for index, line in enumerate(window, start=offset):
        line_offset = char_offset if index == offset else 0
        content = line[line_offset:]
        remaining = MAX_PAGE_CHARS - used_chars
        if len(content) > remaining:
            page.append(content[:remaining])
            if remaining:
                end_line = index
            truncated = True
            # 超长行从当前行的未读字符继续，不能直接跳到下一行而丢失内容。
            next_offset = index
            next_char_offset = line_offset + remaining
            break
        page.append(content)
        used_chars += len(content)
        end_line = index

    return {
        "file_name": source.name,
        "file_path": str(source),
        "markdown_path": str(markdown_path),
        "converted_now": converted_now,
        "total_lines": total_lines,
        "total_chars": total_chars,
        "start_line": offset,
        "char_offset": char_offset,
        "end_line": end_line,
        "content": "".join(page),
        "truncated": truncated,
        "next_offset": next_offset,
        "next_char_offset": next_char_offset if truncated else None,
    }
