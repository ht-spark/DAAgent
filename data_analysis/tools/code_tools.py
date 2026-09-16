"""供 Agent 动态生成并执行 Python 分析代码。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from pydantic import JsonValue

from .artifact_tools import ArtifactWorkspace

__all__ = ["CodeExecutionError", "execute_python_code"]

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "runtime" / "_python_code_runner.py"
_DEFAULT_TIMEOUT_SECONDS = 7_100


class CodeExecutionError(RuntimeError):
    """动态 Python 代码执行失败。"""


def execute_python_code(
    code: str,
    *,
    data_paths: Sequence[str | Path],
    output_dir: str | Path,
    workspace_root: str | Path | None = None,
    python_executable: str | Path | None = None,
    environment_source: str = "host",
    environment_name: str = "MMAgent",
) -> dict[str, JsonValue]:
    """执行模型生成的 Python 代码并收集观察结果。

    代码运行时可以使用 ``DATA_FILES``、``DATA_FILE_MAP``、``OUTPUT_DIR`` 和
    ``ARTIFACTS``。若需要向下一轮返回结构化结果，应给全局变量 ``result``
    赋值。生成文件时优先通过 ``ARTIFACTS.path`` 获取经过校验的目标路径。

    Args:
        code: 要运行的 Python 源代码。
        data_paths: 当前运行授权的数据文件路径。
        output_dir: 当前会话授权的产物目录。
        workspace_root: 当前项目目录；为空表示会话未绑定项目。
        python_executable: 执行分析代码的 Python 解释器。
        environment_source: 环境来源，用于向 Agent 报告。
        environment_name: 环境显示名称，用于向 Agent 报告。

    Returns:
        标准输出、标准错误、结构化结果及当前产物清单。

    Raises:
        ValueError: 代码为空或运行目录无效。
        CodeExecutionError: 代码编译或执行失败。
    """
    if not code.strip():
        raise ValueError("Python 代码不能为空")
    workspace = ArtifactWorkspace(output_dir)
    target_dir = workspace.root

    resolved_paths = [
        Path(path).expanduser().resolve(strict=False)
        for path in data_paths
    ]
    missing_paths = [path.name for path in resolved_paths if not path.is_file()]
    if missing_paths:
        raise FileNotFoundError(f"授权数据文件不存在: {', '.join(missing_paths)}")
    executable = Path(python_executable or sys.executable).expanduser().resolve(strict=True)
    if not executable.is_file():
        raise ValueError("Python 解释器路径无效")
    resolved_workspace = (
        str(Path(workspace_root).resolve(strict=True))
        if workspace_root is not None
        else None
    )
    request = {
        "code": code,
        "data_files": [str(path) for path in resolved_paths],
        "output_dir": str(target_dir),
        "workspace_root": resolved_workspace,
    }
    try:
        completed = subprocess.run(
            [str(executable), "-I", str(_RUNNER_PATH)],
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=target_dir,
            env=_safe_python_environment(),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            shell=False,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise CodeExecutionError("Python 代码执行超时") from exc
    except OSError as exc:
        raise CodeExecutionError(f"无法启动选定的 Python 环境: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2_000:] or "目标 Python 异常退出"
        raise CodeExecutionError(detail)
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CodeExecutionError("目标 Python 没有返回合法执行结果") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        error_type = response.get("error_type", "CodeExecutionError")
        detail = f"{error_type}: {response.get('error') or 'Python 代码执行失败'}"
        captured = response.get("captured")
        if isinstance(captured, str) and captured:
            detail += f"\n运行输出:\n{captured}"
        raise CodeExecutionError(detail)
    runtime = response.get("runtime") if isinstance(response.get("runtime"), dict) else {}
    runtime.update({"source": environment_source, "name": environment_name})
    response["runtime"] = runtime
    response.pop("ok", None)
    return response


def _safe_python_environment() -> dict[str, str]:
    """构造目标解释器所需的最小系统环境。"""
    allowed_names = ("COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR")
    environment = {name: os.environ[name] for name in allowed_names if name in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment
