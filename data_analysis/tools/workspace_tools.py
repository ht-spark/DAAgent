"""限定在项目根目录内的只读文件工具。"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

MAX_WORKSPACE_FILE_BYTES = 500_000
# 限制单次返回给模型的完整文本行数，避免大文件一次占满上下文窗口。
MAX_WORKSPACE_READ_LINES = 2_000
# 限制单次返回给模型的 UTF-8 字节数；只返回完整行，超限后通过 next_offset 续读。
MAX_WORKSPACE_READ_BYTES = 50 * 1024
MAX_WORKSPACE_LIST_ITEMS = 500
MAX_WORKSPACE_SEARCH_MATCHES = 100
SKIPPED_DIRECTORY_NAMES = {
    ".git",
    ".runtime",
    ".venv",
    "node_modules",
    "outputs",
    "__pycache__",
}


class WorkspacePathError(ValueError):
    """项目文件路径无效或越过项目根目录。"""


def resolve_workspace_path(
    workspace_root: str | Path,
    relative_path: str | Path = ".",
) -> Path:
    """将相对路径安全解析到项目根目录内。"""
    root = Path(workspace_root).expanduser().resolve(strict=True)
    raw_path = Path(relative_path)
    if raw_path.is_absolute() or ".." in raw_path.parts:
        raise WorkspacePathError("项目文件路径必须是无 '..' 的相对路径")
    target = (root / raw_path).resolve(strict=False)
    if target != root and root not in target.parents:
        raise WorkspacePathError("项目文件路径超出当前项目目录")
    if target != root and any(
        part.casefold() == ".runtime"
        for part in target.relative_to(root).parts
    ):
        raise WorkspacePathError("不允许访问项目运行时内部目录")
    return target


def resolve_workspace_member_path(
    workspace_root: str | Path,
    file_path: str | Path,
) -> Path:
    """把项目相对路径或项目内绝对路径安全解析到工作区中。"""
    root = Path(workspace_root).expanduser().resolve(strict=True)
    raw_value = str(file_path).strip()
    if raw_value.startswith("@"):
        raw_value = raw_value[1:].strip()
    if not raw_value:
        raise WorkspacePathError("项目文件路径不能为空")
    requested = Path(raw_value).expanduser()
    if requested.is_absolute():
        target = requested.resolve(strict=False)
    else:
        if ".." in requested.parts:
            raise WorkspacePathError("项目文件路径不能包含 '..'")
        target = (root / requested).resolve(strict=False)
    if target == root or root not in target.parents:
        raise WorkspacePathError("项目文件路径超出当前项目目录")
    if any(part.casefold() == ".runtime" for part in target.relative_to(root).parts):
        raise WorkspacePathError("不允许访问项目运行时内部目录")
    return target


def list_workspace_directory(
    workspace_root: str | Path,
    relative_dir: str = ".",
) -> dict[str, Any]:
    """只列出项目目录的直接子目录和文件，供逐层了解项目结构。"""
    root = Path(workspace_root).expanduser().resolve(strict=True)
    directory = resolve_workspace_path(root, relative_dir)
    if not directory.is_dir():
        raise WorkspacePathError("要列出的项目路径不是文件夹")
    directories: list[str] = []
    files: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if path.name.casefold() in SKIPPED_DIRECTORY_NAMES:
            continue
        resolved = path.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            continue
        if len(directories) + len(files) >= MAX_WORKSPACE_LIST_ITEMS:
            truncated = True
            break
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            files.append({"path": relative, "size_bytes": path.stat().st_size})
    return {"directories": directories, "files": files, "truncated": truncated}


def list_workspace_files(
    workspace_root: str | Path,
    relative_dir: str = ".",
    pattern: str = "*",
) -> dict[str, Any]:
    """递归列出项目文件，跳过依赖、版本库和输出目录。"""
    root = Path(workspace_root).resolve(strict=True)
    directory = resolve_workspace_path(root, relative_dir)
    if not directory.is_dir():
        raise WorkspacePathError("要列出的项目路径不是文件夹")
    files: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(root)
        if any(part.casefold() in SKIPPED_DIRECTORY_NAMES for part in relative.parts):
            continue
        if not path.is_file() or not fnmatch.fnmatch(path.name, pattern or "*"):
            continue
        resolved = path.resolve(strict=True)
        if root not in resolved.parents:
            continue
        files.append({"path": relative.as_posix(), "size_bytes": path.stat().st_size})
        if len(files) >= MAX_WORKSPACE_LIST_ITEMS:
            truncated = True
            break
    return {"files": files, "truncated": truncated}


def read_workspace_file(
    workspace_root: str | Path,
    relative_path: str,
    offset: int = 1,
    limit: int = MAX_WORKSPACE_READ_LINES,
) -> dict[str, Any]:
    """按 Pi 的行数与字节双重上限读取项目内 UTF-8 文本文件。"""
    if offset < 1 or not 1 <= limit <= MAX_WORKSPACE_READ_LINES:
        raise ValueError(
            f"offset 必须大于 0，limit 必须在 1 到 {MAX_WORKSPACE_READ_LINES} 之间"
        )
    root = Path(workspace_root).resolve(strict=True)
    target = resolve_workspace_member_path(root, relative_path)
    if not target.is_file():
        raise FileNotFoundError(f"项目文件不存在: {relative_path}")
    resolved = target.resolve(strict=True)
    if root not in resolved.parents:
        raise WorkspacePathError("项目文件链接指向项目目录之外")
    selected: list[str] = []
    selected_bytes = 0
    total_lines = 0
    truncated_by: str | None = None
    first_line_exceeds_limit = False
    try:
        with target.open("r", encoding="utf-8-sig", newline="") as stream:
            for line_number, line in enumerate(stream, start=1):
                total_lines = line_number
                if line_number < offset:
                    continue
                if truncated_by is not None:
                    continue
                if len(selected) >= limit:
                    truncated_by = "lines"
                    continue
                line_bytes = len(line.encode("utf-8"))
                if not selected and line_bytes > MAX_WORKSPACE_READ_BYTES:
                    truncated_by = "bytes"
                    first_line_exceeds_limit = True
                    continue
                if selected_bytes + line_bytes > MAX_WORKSPACE_READ_BYTES:
                    truncated_by = "bytes"
                    continue
                selected.append(line)
                selected_bytes += line_bytes
    except UnicodeDecodeError as exc:
        raise WorkspacePathError("项目文件不是可读取的 UTF-8 文本") from exc

    if total_lines and offset > total_lines:
        raise ValueError(f"offset {offset} 超出文件末尾（共 {total_lines} 行）")
    if total_lines == 0 and offset > 1:
        raise ValueError("offset 超出空文件末尾")
    end_line = offset + len(selected) - 1
    truncated = truncated_by is not None
    return {
        "path": target.relative_to(root).as_posix(),
        "content": "".join(selected),
        "start_line": offset,
        "end_line": end_line,
        "total_lines": total_lines,
        "truncated": truncated,
        "truncated_by": truncated_by,
        "next_offset": (
            end_line + 1
            if truncated and selected
            else offset + 1 if first_line_exceeds_limit else None
        ),
        "first_line_exceeds_limit": first_line_exceeds_limit,
    }


def search_workspace_text(
    workspace_root: str | Path,
    query: str,
    pattern: str = "*",
) -> dict[str, Any]:
    """在项目文本文件中搜索固定字符串并返回有限匹配。"""
    if not query:
        raise ValueError("搜索内容不能为空")
    root = Path(workspace_root).resolve(strict=True)
    matches: list[dict[str, Any]] = []
    listing = list_workspace_files(root, pattern=pattern)
    for item in listing["files"]:
        path = resolve_workspace_path(root, str(item["path"]))
        if path.stat().st_size > MAX_WORKSPACE_FILE_BYTES:
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(lines, start=1):
            if query.casefold() not in line.casefold():
                continue
            matches.append({
                "path": str(item["path"]),
                "line": line_number,
                "text": line[:500],
            })
            if len(matches) >= MAX_WORKSPACE_SEARCH_MATCHES:
                return {
                    "matches": matches,
                    "truncated": True,
                    "files_scanned": len(listing["files"]),
                }
    return {
        "matches": matches,
        "truncated": bool(listing["truncated"]),
        "files_scanned": len(listing["files"]),
    }
