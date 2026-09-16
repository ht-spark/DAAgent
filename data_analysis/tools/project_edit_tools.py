"""限定项目目录的文本创建、精确编辑与开发命令。"""
from __future__ import annotations

import difflib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .workspace_tools import WorkspacePathError, resolve_workspace_path

MAX_TEXT_BYTES = 500_000
MAX_COMMAND_OUTPUT_CHARS = 20_000
BLOCKED_PARTS = {".git", ".venv", "node_modules", "__pycache__", "outputs"}


def _editable_path(workspace_root: str | Path, relative_path: str) -> Path:
    """解析项目内可编辑的普通文本文件路径。"""
    root = Path(workspace_root).resolve(strict=True)
    target = resolve_workspace_path(root, relative_path)
    parts = target.relative_to(root).parts
    if not parts or any(part.casefold() in BLOCKED_PARTS for part in parts):
        raise WorkspacePathError("不允许修改该项目路径")
    if target.name.casefold().startswith(".env"):
        raise WorkspacePathError("不允许修改环境密钥文件")
    if target.is_symlink():
        raise WorkspacePathError("不允许修改符号链接文件")
    return target


def create_workspace_file(workspace_root: str | Path, relative_path: str, content: str) -> dict[str, Any]:
    """仅创建不存在的 UTF-8 项目文件，不覆盖现有内容。"""
    target = _editable_path(workspace_root, relative_path)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise ValueError("文件内容超过大小限制")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(encoded)
    return {"path": relative_path, "created": True, "size_bytes": len(encoded)}


def edit_workspace_file(
    workspace_root: str | Path, relative_path: str, old_text: str, new_text: str
) -> dict[str, Any]:
    """仅在旧文本唯一匹配时修改文件，并返回实际 diff。"""
    target = _editable_path(workspace_root, relative_path)
    if not old_text:
        raise ValueError("old_text 不能为空")
    if not target.is_file() or target.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("目标文件不存在或超过大小限制")
    original = target.read_text(encoding="utf-8-sig")
    matches = original.count(old_text)
    if matches != 1:
        raise ValueError(f"old_text 匹配 {matches} 次，需要唯一匹配")
    updated = original.replace(old_text, new_text, 1)
    encoded = updated.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise ValueError("修改后的文件超过大小限制")
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{relative_path}", tofile=f"b/{relative_path}",
    ))
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
        shutil.copymode(target, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": relative_path, "diff": diff[:MAX_COMMAND_OUTPUT_CHARS]}


def run_project_command(
    workspace_root: str | Path, command: str, python_executable: str | Path
) -> dict[str, Any]:
    """在项目目录执行受控测试、构建或只读 Git 命令。"""
    root = Path(workspace_root).resolve(strict=True)
    allowed = {
        "git status": ["git", "status", "--short"],
        "git diff": ["git", "diff", "--"],
        "python -m pytest": [str(python_executable), "-m", "pytest"],
        "npm run test": ["npm", "run", "test"],
        "npm run build": ["npm", "run", "build"],
    }
    if command not in allowed:
        raise ValueError("只允许 git status、git diff、python -m pytest、npm run test、npm run build")
    if command.startswith("git "):
        repository = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, shell=False, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if repository.returncode != 0 or Path(repository.stdout.strip()).resolve() != root:
            raise ValueError("Git 仓库根目录必须与当前项目目录一致")
    if command.startswith("npm "):
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if npm is None:
            raise FileNotFoundError("当前环境找不到 npm")
        allowed[command][0] = npm
    try:
        completed = subprocess.run(
            allowed[command], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, shell=False,
            env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except subprocess.TimeoutExpired:
        return {"command": command, "exit_code": -1, "stdout": "", "stderr": "命令运行超时"}
    return {
        "command": command, "exit_code": completed.returncode,
        "stdout": completed.stdout[:MAX_COMMAND_OUTPUT_CHARS],
        "stderr": completed.stderr[:MAX_COMMAND_OUTPUT_CHARS],
    }

def run_create_workspace_file(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """校验工具参数后创建项目文件。"""
    if set(arguments) != {"relative_path", "content"} or not all(
        isinstance(arguments[key], str) for key in arguments
    ):
        raise ValueError("create_workspace_file 参数无效")
    return create_workspace_file(context["workspace_root"], arguments["relative_path"], arguments["content"])


def run_edit_workspace_file(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """校验工具参数后精确修改项目文件。"""
    if set(arguments) != {"relative_path", "old_text", "new_text"} or not all(
        isinstance(arguments[key], str) for key in arguments
    ):
        raise ValueError("edit_workspace_file 参数无效")
    return edit_workspace_file(
        context["workspace_root"], arguments["relative_path"],
        arguments["old_text"], arguments["new_text"],
    )


def run_project_command_tool(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """校验工具参数后执行受控项目命令。"""
    if set(arguments) != {"command"} or not isinstance(arguments["command"], str):
        raise ValueError("run_project_command 参数无效")
    return run_project_command(
        context["workspace_root"], arguments["command"], context["python_executable"]
    )