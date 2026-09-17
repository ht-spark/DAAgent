"""限定项目目录的文本创建、精确编辑与开发命令。"""
from __future__ import annotations

import difflib
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..python_environment import resolve_python_environment
from .workspace_tools import WorkspacePathError, resolve_workspace_path

MAX_TEXT_BYTES = 500_000
MAX_COMMAND_OUTPUT_CHARS = 20_000
BLOCKED_PARTS = {".git", ".venv", "node_modules", "__pycache__", "outputs"}
PACKAGE_SPEC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!<>=~,*\[\]-]*$")


def _editable_path(workspace_root: str | Path, relative_path: str) -> Path:
    """解析项目内可编辑的普通文本文件路径。"""
    root = Path(workspace_root).resolve(strict=True)
    requested = Path(relative_path)
    if requested.is_absolute():
        target = requested.resolve(strict=False)
        if target == root or root not in target.parents:
            raise WorkspacePathError("项目文件路径超出当前项目目录")
    else:
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
    """在项目目录执行受控开发或环境命令。"""
    root = Path(workspace_root).resolve(strict=True)
    executable = str(Path(python_executable).resolve(strict=True))
    allowed = {
        "git status": ["git", "status", "--short"],
        "git diff": ["git", "diff", "--"],
        "python -m pytest": [executable, "-m", "pytest"],
        "npm run test": ["npm", "run", "test"],
        "npm run build": ["npm", "run", "build"],
    }
    if command in allowed:
        argv = allowed[command]
    else:
        argv = _environment_command(root, command, executable)
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
        argv[0] = npm
    try:
        environment_names = (
            "PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR",
            "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "HOMEDRIVE", "HOMEPATH",
        )
        completed = subprocess.run(
            argv, cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=1800, shell=False,
            env={name: os.environ[name] for name in environment_names if name in os.environ},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except subprocess.TimeoutExpired:
        return {"command": command, "exit_code": -1, "stdout": "", "stderr": "命令运行超时"}
    return {
        "command": command, "exit_code": completed.returncode,
        "stdout": completed.stdout[:MAX_COMMAND_OUTPUT_CHARS],
        "stderr": completed.stderr[:MAX_COMMAND_OUTPUT_CHARS],
    }


def _environment_command(root: Path, command: str, executable: str) -> list[str]:
    """解析有限的环境创建和依赖安装命令，不交给 shell 解释。"""
    if len(command) > 1000 or any(char in command for char in "\r\n\x00"):
        raise ValueError("项目命令过长或包含非法字符")
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError("项目命令引号不完整") from exc
    if parts[:3] == ["python", "-m", "venv"] and len(parts) == 4:
        return [executable, "-m", "venv", str(_project_environment_path(root, parts[3]))]
    if parts[:2] == ["uv", "venv"] and len(parts) in {2, 3}:
        uv = shutil.which("uv")
        if uv is None:
            raise FileNotFoundError("当前环境找不到 uv")
        return [uv, "venv", str(_project_environment_path(root, parts[2] if len(parts) == 3 else ".venv"))]
    if parts == ["uv", "sync"]:
        if not (root / "pyproject.toml").is_file():
            raise ValueError("uv sync 需要项目中的 pyproject.toml")
        uv = shutil.which("uv")
        if uv is None:
            raise FileNotFoundError("当前环境找不到 uv")
        return [uv, "sync"]
    if parts[:3] == ["conda", "create", "-p"] and 4 <= len(parts) <= 5:
        conda = shutil.which("conda")
        if conda is None:
            raise FileNotFoundError("当前环境找不到 conda")
        version = parts[4:] or ["python=3.11"]
        if not all(re.fullmatch(r"python=3\.\d{1,2}", item) for item in version):
            raise ValueError("conda create 仅支持指定 Python 版本")
        return [conda, "create", "-p", str(_project_environment_path(root, parts[3])), *version, "-y"]
    if parts[:3] == ["python", "-m", "pip"]:
        parts = ["pip", *parts[3:]]
    if parts[:2] in (["pip", "install"], ["uv", "pip"]):
        if parts[:2] == ["uv", "pip"]:
            if len(parts) < 4 or parts[2] != "install":
                raise ValueError("只支持 uv pip install")
            packages = parts[3:]
            uv = shutil.which("uv")
            if uv is None:
                raise FileNotFoundError("当前环境找不到 uv")
            prefix = [uv, "pip", "install", "--python", executable]
        else:
            packages = parts[2:]
            prefix = [executable, "-m", "pip", "install"]
        return [*prefix, *_install_arguments(root, packages)]
    if parts[:2] == ["conda", "install"]:
        conda = shutil.which("conda")
        if conda is None:
            raise FileNotFoundError("当前环境找不到 conda")
        environment = Path(executable).parent.parent
        if not (environment / "conda-meta").is_dir():
            raise ValueError("当前 Python 不是 Conda 环境，请指定项目内 Conda 环境")
        packages = _install_arguments(root, parts[2:])
        if "-r" in packages:
            raise ValueError("conda install 不支持 -r，请直接提供包名")
        return [conda, "install", "-p", str(environment), *packages, "-y"]
    raise ValueError("只允许受控 Git、测试、构建、venv/uv/conda 创建环境及 pip/uv/conda 安装依赖")


def _project_environment_path(root: Path, path: str) -> Path:
    """限制新环境位于项目目录中且不覆盖已有路径。"""
    target = resolve_workspace_path(root, path)
    if target == root or target.exists():
        raise ValueError("环境目录必须是项目内尚不存在的路径")
    return target


def _install_arguments(root: Path, parts: list[str]) -> list[str]:
    """只允许包规格、升级开关和项目内需求文件。"""
    if not parts:
        raise ValueError("安装命令需要包名或 -r 需求文件")
    result: list[str] = []
    index = 0
    while index < len(parts):
        item = parts[index]
        if item == "--upgrade":
            result.append(item)
        elif item == "-r" and index + 1 < len(parts):
            target = resolve_workspace_path(root, parts[index + 1])
            if not target.is_file():
                raise ValueError("项目内需求文件不存在")
            result.extend(["-r", str(target)])
            index += 1
        elif PACKAGE_SPEC.fullmatch(item):
            result.append(item)
        else:
            raise ValueError(f"不支持的安装参数: {item}")
        index += 1
    return result

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
    if set(arguments) - {"command", "python_environment"} or not isinstance(arguments.get("command"), str):
        raise ValueError("run_project_command 参数无效")
    executable = context["python_executable"]
    requested_environment = arguments.get("python_environment")
    if requested_environment is not None:
        if not isinstance(requested_environment, str) or not requested_environment.strip():
            raise ValueError("python_environment 必须是项目内环境路径")
        root = Path(context["workspace_root"]).resolve(strict=True)
        requested = Path(requested_environment)
        candidate = requested if requested.is_absolute() else root / requested
        candidate = candidate.resolve(strict=True)
        if candidate != root and root not in candidate.parents:
            raise WorkspacePathError("命令目标环境必须位于当前项目内")
        executable = str(resolve_python_environment(root, candidate).executable)
    return run_project_command(
        context["workspace_root"], arguments["command"], executable
    )
