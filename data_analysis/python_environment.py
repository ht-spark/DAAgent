"""发现、声明读取与创建项目使用的 Python 虚拟环境。

解析优先级：
1. 项目 AGENTS.md 中声明的 python_environment（最高约束，声明无效直接报错）；
2. 用户显式指定的路径（存在则校验使用，不存在则作为创建位置）；
3. 扫描项目根目录下一层的现有虚拟环境；
4. 都没有时自动创建虚拟环境（默认项目根目录 .venv）。
任何情况下都不会回退到宿主解释器执行项目代码。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PythonEnvironment",
    "PythonEnvironmentError",
    "resolve_python_environment",
]

# AGENTS.md 中环境声明的行格式：python_environment: <路径> 或 python_environment=<路径>
_AGENTS_MD_ENV_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?python_environment\s*[:=]\s*(\S.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


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
    """按 AGENTS.md 声明、用户配置、项目扫描、自动创建的顺序选择解释器。

    Args:
        workspace_root: 当前项目根目录；为空时无法读取声明、扫描或创建环境。
        configured_path: 用户指定的虚拟环境目录或 Python 可执行文件；
            路径不存在时会被视为期望的虚拟环境创建位置。

    Returns:
        已解析且真实存在的 Python 环境。

    Raises:
        PythonEnvironmentError: AGENTS.md 声明无效、用户路径无法使用，
            或没有项目根目录可供发现与创建环境。
    """
    has_configured = configured_path is not None and str(configured_path).strip()
    root = (
        Path(workspace_root).expanduser().resolve(strict=True)
        if workspace_root is not None
        else None
    )

    # 优先级 1：AGENTS.md 声明是最高约束，声明无效时直接报错而非静默回退。
    if root is not None:
        declared = _read_agents_md_environment(root)
        if declared is not None:
            return _environment_from_path(declared, "declared")

    # 优先级 2：用户显式指定的环境；路径不存在时作为创建位置。
    if has_configured:
        configured = Path(str(configured_path).strip()).expanduser()
        if configured.exists():
            return _environment_from_path(configured, "custom")
        return _create_project_environment(root, configured)

    # 优先级 3：扫描项目根目录下一层的现有虚拟环境。
    if root is not None:
        for candidate in _project_environment_roots(root):
            executable = _candidate_executable(candidate)
            if executable.is_file():
                return PythonEnvironment(
                    executable.resolve(strict=True),
                    "project",
                    candidate.name,
                )

        # 优先级 4：项目内没有环境时，在项目根目录自动创建 .venv。
        if (root / ".venv").exists():
            raise PythonEnvironmentError(
                "项目内存在 .venv 目录但未找到可用的 Python 解释器，"
                "请修复或删除该目录后重试。"
            )
        return _create_project_environment(root, root / ".venv")

    raise PythonEnvironmentError(
        "未指定 Python 环境且没有项目根目录可供发现或创建虚拟环境。"
    )


def _environment_from_path(raw_path: Path, source: str) -> PythonEnvironment:
    """校验并封装一个已存在的环境路径。"""
    executable = _resolve_executable(raw_path)
    environment_root = (
        executable.parent.parent
        if executable.parent.name.lower() in {"bin", "scripts"}
        else executable.parent
    )
    return PythonEnvironment(executable, source, environment_root.name)


def _read_agents_md_environment(root: Path) -> Path | None:
    """读取 AGENTS.md 中声明的虚拟环境路径；未声明时返回 None。

    支持的行格式（不区分大小写）：
        python_environment: D:\\path\\to\\.venv
        python_environment=D:\\path\\to\\.venv
    相对路径会相对项目根目录解析。
    """
    target = root / "AGENTS.md"
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _AGENTS_MD_ENV_PATTERN.search(text)
    if match is None:
        return None
    raw = match.group(1).strip().strip("\"'`")
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate


def _create_project_environment(
    root: Path | None, target: Path
) -> PythonEnvironment:
    """在指定位置创建虚拟环境并返回封装结果。

    仅使用宿主解释器执行 `python -m venv` 引导创建；后续项目代码
    一律使用新创建的环境执行，不会回退到宿主。
    """
    if root is None:
        raise PythonEnvironmentError("没有项目根目录，无法创建虚拟环境")
    resolved_target = target.expanduser()
    if not resolved_target.is_absolute():
        resolved_target = root / resolved_target
    if resolved_target.exists():
        if (resolved_target / "pyvenv.cfg").is_file():
            return _environment_from_path(resolved_target, "created")
        raise PythonEnvironmentError(
            f"创建位置已存在且不是虚拟环境目录: {resolved_target}"
        )
    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "venv", str(resolved_target)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise PythonEnvironmentError(f"无法启动虚拟环境创建: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-500:] or "venv 创建异常退出"
        raise PythonEnvironmentError(f"虚拟环境创建失败: {detail}")
    return _environment_from_path(resolved_target, "created")


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
