"""限定在项目根目录内的只读文件工具。"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

MAX_WORKSPACE_FILE_BYTES = 500_000
MAX_WORKSPACE_LIST_ITEMS = 500
MAX_WORKSPACE_SEARCH_MATCHES = 100
SKIPPED_DIRECTORY_NAMES = {".git", ".venv", "node_modules", "outputs", "__pycache__"}


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
    workspace_root: str | Path, relative_path: str, offset: int = 1, limit: int = 500
) -> dict[str, Any]:
    """读取项目内一个有限大小的 UTF-8 文本文件。"""
    if offset < 1 or not 1 <= limit <= 500:
        raise ValueError("offset 必须大于 0，limit 必须在 1 到 500 之间")
    root = Path(workspace_root).resolve(strict=True)
    target = resolve_workspace_path(root, relative_path)
    if not target.is_file():
        raise FileNotFoundError(f"项目文件不存在: {relative_path}")
    resolved = target.resolve(strict=True)
    if root not in resolved.parents:
        raise WorkspacePathError("项目文件链接指向项目目录之外")
    content = target.read_bytes()
    truncated = len(content) > MAX_WORKSPACE_FILE_BYTES
    try:
        text = content[:MAX_WORKSPACE_FILE_BYTES].decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkspacePathError("项目文件不是可读取的 UTF-8 文本") from exc
    lines = text.splitlines(keepends=True)
    selected = lines[offset - 1:offset - 1 + limit]
    return {
        "path": target.relative_to(root).as_posix(),
        "content": "".join(selected),
        "start_line": offset,
        "end_line": offset + len(selected) - 1,
        "total_lines": len(lines),
        "truncated": truncated or offset - 1 + limit < len(lines),
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
    for item in list_workspace_files(root, pattern=pattern)["files"]:
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
                return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": False}