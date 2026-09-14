"""读取 Agent 已生成的文本与代码产物。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from .registry import ToolArgumentsError

__all__ = ["inspect_artifact"]

MAX_INSPECT_ARTIFACT_CHARS = 50_000
_INSPECTABLE_SUFFIXES = frozenset(
    {
        ".css",
        ".csv",
        ".html",
        ".js",
        ".json",
        ".jsx",
        ".markdown",
        ".md",
        ".py",
        ".r",
        ".sql",
        ".tex",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


def inspect_artifact(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """读取当前会话产物目录内的文本或代码文件。

    Args:
        arguments: 包含产物相对路径的工具参数。
        context: 包含当前产物目录的运行上下文。

    Returns:
        文件内容、大小及是否截断。

    Raises:
        ToolArgumentsError: 参数、路径或文件格式不符合约定。
    """
    if set(arguments) != {"relative_path"}:
        raise ToolArgumentsError("inspect_artifact 只接受必填参数 relative_path")
    relative_path = arguments.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ToolArgumentsError("inspect_artifact 缺少有效的 relative_path")
    output_dir = context.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ToolArgumentsError("inspect_artifact 缺少有效的产物目录上下文")

    target = resolve_generated_artifact(output_dir, relative_path.strip())
    if target.suffix.lower() not in _INSPECTABLE_SUFFIXES:
        raise ToolArgumentsError(f"产物不是可读取的文本或代码格式: {relative_path}")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolArgumentsError(f"产物不是 UTF-8 文本: {relative_path}") from exc
    truncated = len(content) > MAX_INSPECT_ARTIFACT_CHARS
    if truncated:
        content = content[:MAX_INSPECT_ARTIFACT_CHARS] + "\n…内容已截断"
    return {
        "path": target.relative_to(Path(output_dir).resolve()).as_posix(),
        "content": content,
        "size_bytes": target.stat().st_size,
        "truncated": truncated,
    }


def resolve_generated_artifact(output_dir: str | Path, relative_path: str) -> Path:
    """解析并校验当前会话内已经存在的产物路径。"""
    root = Path(output_dir).expanduser().resolve(strict=False)
    raw_path = Path(relative_path)
    if raw_path.is_absolute() or not raw_path.parts or ".." in raw_path.parts:
        raise ToolArgumentsError("产物路径必须是无 '..' 的非空相对路径")
    if raw_path.parts[0].casefold() in {"input", ".runs"}:
        raise ToolArgumentsError("禁止读取输入附件目录")
    target = (root / raw_path).resolve(strict=False)
    if target == root or root not in target.parents:
        raise ToolArgumentsError("产物路径超出当前会话产物目录")
    if not target.is_file():
        raise ToolArgumentsError(f"产物不存在或不是普通文件: {relative_path}")
    return target
