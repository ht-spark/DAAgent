"""基于真实文件夹的项目注册与项目级上下文。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from data_analysis.python_environment import (
    PythonEnvironment,
    PythonEnvironmentError,
    resolve_python_environment,
)
from server import store

MAX_PROJECT_CONTEXT_CHARS = 60_000
PROJECT_CONTEXT_FILES = ("AGENTS.md", "Project.md")


class ProjectWorkspaceError(ValueError):
    """项目工作区无效。"""


def canonicalize_project_root(raw_path: str) -> tuple[Path, str]:
    """解析真实项目目录并返回用于去重的规范键。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ProjectWorkspaceError("项目文件夹路径不能为空")
    candidate = Path(raw_path.strip()).expanduser()
    if not candidate.is_absolute():
        raise ProjectWorkspaceError("项目文件夹必须使用绝对路径")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProjectWorkspaceError("项目文件夹不存在或无法访问") from exc
    if not root.is_dir():
        raise ProjectWorkspaceError("项目路径必须指向文件夹")
    return root, os.path.normcase(str(root))


def create_project(raw_path: str, title: str | None = None) -> dict[str, Any]:
    """注册一个真实文件夹；相同目录只保留一条项目记录。"""
    root, root_key = canonicalize_project_root(raw_path)
    existing = store.get_project_by_root_key(root_key)
    if existing is not None:
        return existing
    normalized_title = (title or root.name or str(root)).strip()[:80]
    project_id = store.create_project(normalized_title, str(root), root_key)
    project = store.get_project(project_id)
    if project is None:
        raise RuntimeError("项目注册后无法读取")
    return project


def require_project(project_id: str) -> dict[str, Any]:
    """返回仍然有效的已注册项目。"""
    project = store.get_project(project_id)
    if project is None:
        raise ProjectWorkspaceError("项目不存在或已被移除")
    root, root_key = canonicalize_project_root(str(project["root_path"]))
    if root_key != project["root_key"]:
        raise ProjectWorkspaceError("项目文件夹规范路径已经变化，请重新注册")
    return {**project, "root_path": str(root)}


def resolve_project_python(project: dict[str, Any]) -> PythonEnvironment:
    """解析一个项目当前实际使用的 Python 环境。"""
    try:
        return resolve_python_environment(
            project["root_path"],
        )
    except (OSError, PythonEnvironmentError) as exc:
        raise ProjectWorkspaceError(str(exc)) from exc


def load_project_context(root_path: str | Path) -> str | None:
    """读取项目根目录中有限长度的项目说明文件。"""
    root = Path(root_path).resolve(strict=True)
    sections: list[str] = []
    remaining = MAX_PROJECT_CONTEXT_CHARS
    for name in PROJECT_CONTEXT_FILES:
        target = (root / name).resolve(strict=False)
        if target.parent != root or not target.is_file() or remaining <= 0:
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        excerpt = text[:remaining].strip()
        if excerpt:
            sections.append(f"## {name}\n{excerpt}")
            remaining -= len(excerpt)
    return "\n\n".join(sections) or None
