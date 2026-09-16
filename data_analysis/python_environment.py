"""发现并校验项目使用的 Python 虚拟环境。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PythonEnvironment",
    "PythonEnvironmentError",
    "resolve_python_environment",
]


class PythonEnvironmentError(ValueError):
    """Python 环境路径无效。"""


@dataclass(frozen=True)
class PythonEnvironment:
    """一次运行最终选定的 Python 解释器。"""

    executable: Path
    source: str
    name: str


def resolve_python_environment(
    workspace_root: str | Path | None,
    configured_path: str | Path | None = None,
) -> PythonEnvironment:
    """按用户配置、项目虚拟环境和主环境的顺序选择解释器。

    Args:
        workspace_root: 当前项目根目录；为空时不能自动发现项目环境。
        configured_path: 用户指定的虚拟环境目录或 Python 可执行文件。

    Returns:
        已解析且真实存在的 Python 环境。

    Raises:
        PythonEnvironmentError: 用户指定路径无效。
    """
    if configured_path is not None and str(configured_path).strip():
        executable = _resolve_executable(Path(str(configured_path).strip()))
        environment_root = (
            executable.parent.parent
            if executable.parent.name.lower() in {"bin", "scripts"}
            else executable.parent
        )
        return PythonEnvironment(executable, "custom", environment_root.name)

    if workspace_root is not None:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        for candidate in _project_environment_roots(root):
            executable = _candidate_executable(candidate)
            if executable.is_file():
                return PythonEnvironment(
                    executable.resolve(strict=True),
                    "project",
                    candidate.name,
                )

    executable = Path(sys.executable).resolve(strict=True)
    return PythonEnvironment(executable, "host", "MMAgent")


def _resolve_executable(raw_path: Path) -> Path:
    """把虚拟环境目录或解释器路径规范化为真实解释器。"""
    candidate = raw_path.expanduser()
    if not candidate.is_absolute():
        raise PythonEnvironmentError("Python 环境必须使用绝对路径")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PythonEnvironmentError("指定的 Python 环境不存在或无法访问") from exc
    executable = _candidate_executable(resolved) if resolved.is_dir() else resolved
    if not executable.is_file():
        raise PythonEnvironmentError("指定路径中没有找到 Python 可执行文件")
    if os.name == "nt" and executable.name.lower() not in {"python.exe", "python3.exe"}:
        raise PythonEnvironmentError("Windows 环境必须指向 python.exe 或虚拟环境目录")
    if os.name != "nt" and not executable.name.lower().startswith("python"):
        raise PythonEnvironmentError("环境必须指向 Python 可执行文件或虚拟环境目录")
    return executable.resolve(strict=True)


def _candidate_executable(environment_root: Path) -> Path:
    """返回一个虚拟环境目录对应的平台解释器路径。"""
    if os.name == "nt":
        return environment_root / "Scripts" / "python.exe"
    return environment_root / "bin" / "python"


def _project_environment_roots(root: Path) -> list[Path]:
    """按稳定优先级扫描项目根目录下一层的虚拟环境。"""
    preferred = [root / name for name in (".venv", "venv", "env")]
    preferred_keys = {os.path.normcase(str(path)) for path in preferred}
    try:
        discovered = sorted(
            (
                path
                for path in root.iterdir()
                if path.is_dir()
                and (path / "pyvenv.cfg").is_file()
                and os.path.normcase(str(path)) not in preferred_keys
            ),
            key=lambda path: path.name.lower(),
        )
    except OSError:
        discovered = []
    return preferred + discovered

