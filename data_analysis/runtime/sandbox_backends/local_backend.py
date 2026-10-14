"""local 后端：封装既有 subprocess runner，保持与迁移前行为完全一致。

设计说明（对齐设计方案 §7 的递归陷阱提醒）：

- 本模块直接承载原 ``tools/code_tools.py`` 内的执行逻辑（搬运而非回调），
  避免 ``SandboxService`` 路由与旧入口之间形成递归调用；
- 行为差异为零：错误分类、响应结构、缓存目录与超时处理均与迁移前一致；
- ``run_command`` 在 Phase 2（终端接入）之前不接线，显式抛
  ``NotImplementedError``；终端工具当前仍走既有直连路径，行为不变。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from .base import BackendProbe, PythonRunRequest, CommandRunRequest, SandboxPolicy
from .errors import CodeExecutionError

__all__ = ["LocalBackend"]

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "_python_code_runner.py"


class LocalBackend:
    """现有 subprocess runner 的后端封装（默认后端与回退路径）。"""

    name = "local"

    def probe(self) -> BackendProbe:
        """local 后端始终可用（使用当前解释器启动子进程）。"""
        return BackendProbe(available=True, backend=self.name, tier="subprocess")

    def run_python(
        self,
        request: PythonRunRequest,
        *,
        policy: SandboxPolicy,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        """在独立子进程中执行 Python 代码。

        执行逻辑与迁移前的 ``tools/code_tools.execute_python_code`` 保持一致；
        ``cancel_check`` 参数为 Phase 2 取消增强预留，当前不使用。
        """
        code = request.code
        if not code.strip():
            raise ValueError("Python 代码不能为空")
        # 与既有 ArtifactWorkspace 相同的产物根目录解析与创建
        target_dir = Path(request.output_dir).expanduser().resolve(strict=False)
        target_dir.mkdir(parents=True, exist_ok=True)
        if not target_dir.is_dir():
            raise ValueError(f"产物目录无效: {target_dir}")

        resolved_paths = [
            Path(path).expanduser().resolve(strict=False)
            for path in request.data_paths
        ]
        missing_paths = [path.name for path in resolved_paths if not path.is_file()]
        if missing_paths:
            raise FileNotFoundError(f"授权数据文件不存在: {', '.join(missing_paths)}")
        executable = Path(request.python_executable).expanduser().resolve(strict=True)
        if not executable.is_file():
            raise ValueError("Python 解释器路径无效")
        resolved_workspace = (
            str(Path(request.workspace_root).resolve(strict=True))
            if request.workspace_root is not None
            else None
        )
        payload = {
            "code": code,
            "data_files": [str(path) for path in resolved_paths],
            "output_dir": str(target_dir),
            "workspace_root": resolved_workspace,
            "skill_assets": dict(request.skill_assets or {}),
        }
        try:
            completed = subprocess.run(
                [str(executable), "-I", "-B", str(_RUNNER_PATH)],
                input=json.dumps(payload, ensure_ascii=True),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=target_dir,
                env=_safe_python_environment(target_dir),
                timeout=request.timeout_seconds,
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
        changed = response.get("changed_artifacts")
        if not isinstance(changed, list) or not all(
            isinstance(item, str) for item in changed
        ):
            raise CodeExecutionError("目标 Python 返回的产物清单无效")
        expected = {_normalize_expected_artifact(path) for path in request.expected_artifacts}
        missing = sorted(expected - set(changed))
        if missing:
            raise CodeExecutionError(
                "代码执行成功，但没有在当前运行目录生成承诺产物: "
                + ", ".join(missing)
            )
        runtime = response.get("runtime") if isinstance(response.get("runtime"), dict) else {}
        runtime.update(
            {"source": request.environment_source, "name": request.environment_name}
        )
        response["runtime"] = runtime
        response.pop("ok", None)
        return response

    def run_command(
        self,
        request: CommandRunRequest,
        *,
        policy: SandboxPolicy,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        """终端命令执行在 Phase 2 接入（当前终端工具走既有直连路径）。"""
        raise NotImplementedError("终端命令的后端化将在 Phase 2 实施")


def _normalize_expected_artifact(relative_path: str) -> str:
    """校验模型声明的产物路径是当前输出目录内的相对路径。"""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("expected_artifacts 只能包含非空相对路径")
    candidate = Path(relative_path.strip())
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("expected_artifacts 只能包含无 '..' 的相对路径")
    return candidate.as_posix()


def _safe_python_environment(output_dir: Path) -> dict[str, str]:
    """构造目标解释器所需的最小环境，并把运行缓存限制在本 Run。"""
    runtime_dir = output_dir / ".runtime"
    temporary_dir = runtime_dir / "tmp"
    temporary_dir.mkdir(parents=True, exist_ok=True)
    allowed_names = ("COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR")
    environment = {name: os.environ[name] for name in allowed_names if name in os.environ}
    environment.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": str(temporary_dir),
        "TMP": str(temporary_dir),
        "MPLCONFIGDIR": str(runtime_dir / "matplotlib"),
        "XDG_CACHE_HOME": str(runtime_dir / "cache"),
        "NUMBA_CACHE_DIR": str(runtime_dir / "numba"),
        "JOBLIB_TEMP_FOLDER": str(temporary_dir),
    })
    return environment
