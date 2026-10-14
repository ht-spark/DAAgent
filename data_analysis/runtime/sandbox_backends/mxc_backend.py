"""MXC 后端：通过 ``wxc-exec.exe`` + JSON 配置在 OS 级沙箱中执行代码。

对齐设计方案 §5 的完整流程：

1. probe 准入（执行器存在性 + ``--probe`` 输出；``appcontainer-dacl`` 等降级
   tier 在 ``require_supported_host`` 下视为不可用）；
2. 控制面/产物面分离：payload / wrapper / config 写入宿主私有控制目录
   （位于任何沙箱授权读写路径之外，沙箱只读挂载）；sidecar 结果写在本任务
   授权输出目录内；
3. 宿主侧产物快照（before）→ ``wxc-exec.exe`` 执行（宿主超时 + 取消双保险）；
4. 读取 sidecar 并**按不可信数据**校验（大小/schema/run_id 关联/字段类型）；
5. 宿主侧产物快照（after）→ 变化清单 → SHA-256 结构化描述；
6. ``expected_artifacts`` 承诺校验；
7. 组装与既有 runner 一致的响应结构；失败抛 ``CodeExecutionError``。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Mapping

from ...cancellation import run_cancellable_process
from ._mxc_task_wrapper import (
    CONTEXT_FILE_NAME,
    SIDECAR_SCHEMA_VERSION,
    WRAPPER_FILE_NAME,
    write_task_files,
)
from .artifacts import (
    ArtifactLimitError,
    changed_artifacts,
    describe_artifacts,
    normalize_relative_path,
    snapshot_artifacts,
)
from .base import BackendProbe, CommandRunRequest, ExecutionLimits, PythonRunRequest, SandboxPolicy
from .errors import (
    CodeExecutionError,
    SandboxUnavailableError,
    SidecarProtocolError,
)
from .trace import emit_sandbox_event

__all__ = [
    "MxcBackend",
    "build_mxc_config",
    "parse_probe_output",
    "resolve_runtime_dirs",
]

# 与所使用执行器支持的 schema 精确匹配（实测于 @microsoft/mxc-sdk@1.0.0）
CONFIG_CONTRACT_VERSION = "1.0.0"
# 宿主持久化配置目录名（位于系统临时目录下，沙箱 RW 授权之外）
_CONTROL_DIR_NAME = "daagent-mxc"
_CONFIG_FILE_NAME = "config.json"
# 官方支持矩阵外的已知降级 tier（Windows 10 实测值）；require_supported_host 时拒绝
_NON_SUPPORTED_TIERS = frozenset({"appcontainer-dacl"})
# 宿主进程超时比容器超时多出的缓冲（秒），确保容器侧超时先生效、错误更干净
_HOST_TIMEOUT_GRACE_SECONDS = 60.0


def parse_probe_output(stdout: str) -> BackendProbe:
    """解析 ``wxc-exec.exe --probe`` 的 JSON 输出。

    Raises:
        ValueError: 输出不是合法 JSON 对象或缺少必需字段。
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("probe 输出不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("probe 输出不是 JSON 对象")
    tier = payload.get("tier")
    if not isinstance(tier, str) or not tier:
        raise ValueError("probe 输出缺少 tier 字段")
    warnings = payload.get("warnings")
    warning_items = tuple(
        str(item) for item in warnings if isinstance(warnings, list)
    ) if isinstance(warnings, list) else ()
    return BackendProbe(
        available=True,
        backend="mxc",
        tier=tier,
        warnings=warning_items,
    )


def resolve_runtime_dirs(python_executable: Path) -> tuple[Path, Path | None]:
    """从解释器路径反推运行时只读授权目录。

    Returns:
        ``(venv_root, base_python_dir)``：
        - venv 布局（存在 ``pyvenv.cfg``）：venv 根目录 + ``home`` 声明的
          base 解释器目录；
        - 直接解释器：解释器所在目录 + ``None``。
    """
    executable = python_executable.resolve()
    venv_root = executable.parent.parent
    config = venv_root / "pyvenv.cfg"
    if not config.is_file():
        return executable.parent, None
    base_dir: Path | None = None
    try:
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip().casefold() == "home":
                candidate = Path(value.strip()).expanduser()
                if candidate.is_dir():
                    base_dir = candidate.resolve()
                break
    except OSError:
        base_dir = None
    if base_dir is None:
        # 声明无效时退回到解释器所在目录（最小授权，不放大到用户主目录）
        base_dir = executable.parent
    return venv_root, base_dir


def build_mxc_config(
    request: PythonRunRequest,
    *,
    policy: SandboxPolicy,
    control_dir: Path,
    sidecar_path: Path,
    executable: Path,
    output_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """构造一次性容器执行的 JSON 配置（对齐设计方案 §5.2/§5.3 映射表）。

    Args:
        request: 本次执行请求。
        policy: 受信任执行策略（控制网络与上限）。
        control_dir: 宿主私有控制目录（只读挂载进沙箱）。
        sidecar_path: 结果回传文件路径（位于授权输出目录内）。
        executable: 项目虚拟环境的 Python 解释器（已解析为绝对路径）。
        output_dir: 本任务唯一授权读写输出目录（已解析）。
        environ: 宿主环境映射（默认 ``os.environ``，仅用于提取系统必需变量）。
    """
    source = os.environ if environ is None else environ
    readonly: list[str] = []
    for path in request.data_paths:
        readonly.append(str(Path(path).expanduser().resolve(strict=False)))
    for path in (request.skill_assets or {}).values():
        readonly.append(str(Path(path).expanduser().resolve(strict=False)))
    readonly.append(str(control_dir))
    venv_root, base_dir = resolve_runtime_dirs(executable)
    readonly.append(str(venv_root))
    if base_dir is not None:
        readonly.append(str(base_dir))
    # 去重且保持顺序（执行器对重复路径的容忍度未经验证，保守去重）
    readonly_paths = list(dict.fromkeys(readonly))

    # Windows 必需项：LOCALAPPDATA 为 presence-only（实测缺失直接拒绝启动）
    local_app_data = (
        source.get("LOCALAPPDATA")
        or str(Path(source.get("USERPROFILE") or str(Path.home())) / "AppData" / "Local")
    )
    system_root = source.get("SYSTEMROOT") or source.get("SystemRoot") or r"C:\Windows"
    runtime_cache = output_dir / ".runtime"
    env_entries = [
        f"SYSTEMROOT={system_root}",
        f"LOCALAPPDATA={local_app_data}",
        # TEMP/TMP 由平台管理（实测重定向到容器私有目录），缓存改用显式变量
        f"MPLCONFIGDIR={runtime_cache / 'matplotlib'}",
        "PYTHONUTF8=1",
        "PYTHONIOENCODING=utf-8",
        "PYTHONDONTWRITEBYTECODE=1",
    ]
    task_script = control_dir / WRAPPER_FILE_NAME
    return {
        "version": CONFIG_CONTRACT_VERSION,
        "containerId": f"daagent-{request.run_id}",
        "containment": "process",
        "lifecycle": {"destroyOnExit": True, "preservePolicy": False},
        "process": {
            "commandLine": f'"{executable}" -I -B "{task_script}"',
            "cwd": str(output_dir),
            "timeout": max(1, int(request.timeout_seconds * 1000)),
            "env": env_entries,
            "inheritDefaultEnv": False,
        },
        "filesystem": {
            "readwritePaths": [str(output_dir)],
            "readonlyPaths": readonly_paths,
            "deniedPaths": [],
        },
        "network": {
            "egress": {"default": "allow" if policy.allow_network else "deny"}
        },
        # 跑解释器必需（实测：缺省时 python.exe 因 Win32k 被拦报 0xC0000142）
        "ui": {"disable": False},
    }


class MxcBackend:
    """通过 ``wxc-exec.exe`` 执行一次性隔离任务的 MXC 后端。"""

    name = "mxc"

    def __init__(
        self,
        executable: str | Path,
        *,
        require_supported_host: bool = False,
        control_root: str | Path | None = None,
    ) -> None:
        """初始化后端。

        Args:
            executable: ``wxc-exec.exe`` 路径（由服务端配置提供）。
            require_supported_host: 为 True 时把官方支持矩阵外的降级 tier
                视为不可用（生产准入用）。
            control_root: 控制目录根；默认 ``<系统临时目录>/daagent-mxc``。
        """
        self._executable = Path(executable).expanduser()
        self._require_supported_host = require_supported_host
        root = (
            Path(control_root).expanduser()
            if control_root is not None
            else Path(tempfile.gettempdir()) / _CONTROL_DIR_NAME
        )
        self._control_root = root
        self._probe_cache: BackendProbe | None = None

    def probe(self) -> BackendProbe:
        """探测执行器可用性并缓存结果。"""
        if self._probe_cache is not None:
            return self._probe_cache
        if not self._executable.is_file():
            self._probe_cache = BackendProbe(
                available=False,
                backend=self.name,
                reason=f"MXC 执行器不存在: {self._executable}",
            )
            return self._probe_cache
        try:
            completed = subprocess.run(
                [str(self._executable), "--probe"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                shell=False,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._probe_cache = BackendProbe(
                available=False,
                backend=self.name,
                reason=f"probe 执行失败: {exc}",
            )
            return self._probe_cache
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-500:]
            self._probe_cache = BackendProbe(
                available=False,
                backend=self.name,
                reason=f"probe 失败（状态码 {completed.returncode}）: {detail or '无输出'}",
            )
            return self._probe_cache
        try:
            result = parse_probe_output(completed.stdout)
        except ValueError as exc:
            self._probe_cache = BackendProbe(
                available=False,
                backend=self.name,
                reason=str(exc),
            )
            return self._probe_cache
        if self._require_supported_host and result.tier in _NON_SUPPORTED_TIERS:
            self._probe_cache = BackendProbe(
                available=False,
                backend=self.name,
                tier=result.tier,
                reason=(
                    f"当前宿主 tier 为 {result.tier}，不在官方支持矩阵内"
                    "（DAAGENT_MXC_REQUIRE_SUPPORTED_HOST=true）"
                ),
                warnings=result.warnings,
            )
            return self._probe_cache
        self._probe_cache = result
        return result

    def run_python(
        self,
        request: PythonRunRequest,
        *,
        policy: SandboxPolicy,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        """在 MXC 沙箱中执行一次 Python 代码（流程见模块 docstring）。"""
        probe = self.probe()
        if not probe.available:
            raise SandboxUnavailableError(f"MXC 后端不可用: {probe.reason}")

        # ---- 1) 前置校验（错误语义与 local 后端保持一致） ----
        if not request.code.strip():
            raise ValueError("Python 代码不能为空")
        output_dir = Path(request.output_dir).expanduser().resolve(strict=False)
        output_dir.mkdir(parents=True, exist_ok=True)
        if not output_dir.is_dir():
            raise ValueError(f"产物目录无效: {output_dir}")
        executable = Path(request.python_executable).expanduser().resolve(strict=True)
        if not executable.is_file():
            raise ValueError("Python 解释器路径无效")
        resolved_data = [
            Path(path).expanduser().resolve(strict=False)
            for path in request.data_paths
        ]
        missing_paths = [path.name for path in resolved_data if not path.is_file()]
        if missing_paths:
            raise FileNotFoundError(f"授权数据文件不存在: {', '.join(missing_paths)}")
        resolved_workspace = (
            str(Path(request.workspace_root).resolve(strict=True))
            if request.workspace_root is not None
            else None
        )
        skill_assets = {
            str(key): str(Path(value).expanduser().resolve(strict=False))
            for key, value in (request.skill_assets or {}).items()
        }

        # ---- 2) 控制面/产物面分离与任务文件生成 ----
        control_dir = self._control_root / request.run_id
        sidecar_path = output_dir / ".runtime" / "mxc" / request.run_id / "result.json"
        _assert_control_isolation(control_dir, output_dir)
        # 幂等：同 run_id 重试时先清掉旧控制目录（唯一 ID，正常路径不冲突）
        if control_dir.exists():
            shutil.rmtree(control_dir, ignore_errors=True)
        (output_dir / ".runtime" / "matplotlib").mkdir(parents=True, exist_ok=True)
        write_task_files(
            control_dir,
            run_id=request.run_id,
            code=request.code,
            data_files=[str(path) for path in resolved_data],
            output_dir=str(output_dir),
            workspace_root=resolved_workspace,
            skill_assets=skill_assets,
            sidecar_path=str(sidecar_path),
        )
        config = build_mxc_config(
            request,
            policy=policy,
            control_dir=control_dir,
            sidecar_path=sidecar_path,
            executable=executable,
            output_dir=output_dir,
        )
        config_path = control_dir / _CONFIG_FILE_NAME
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # ---- 3) 宿主侧快照与执行 ----
        before = snapshot_artifacts(output_dir)
        emit_sandbox_event(
            "mxc.run.start",
            run_id=request.run_id,
            tier=probe.tier,
            timeout_seconds=request.timeout_seconds,
            readonly_count=len(config["filesystem"]["readonlyPaths"]),
        )
        try:
            completed = run_cancellable_process(
                [str(self._executable), str(config_path)],
                cancel_check=cancel_check,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(output_dir),
                # 宿主超时略大于容器超时：让容器侧先生效，错误更干净
                timeout=request.timeout_seconds + _HOST_TIMEOUT_GRACE_SECONDS,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise CodeExecutionError("Python 代码执行超时") from exc
        except OSError as exc:
            raise CodeExecutionError(f"无法启动 MXC 执行器: {exc}") from exc
        finally:
            # 控制目录包含用户代码副本：任何情况下宿主侧立即清理（容错）
            self._cleanup_control_dir(control_dir, run_id=request.run_id)

        # ---- 4) 宿主执行状态（优先于 sidecar）----
        stdout_text = _trim(completed.stdout or "", policy.limits.max_stdout_bytes)
        stderr_text = _trim(completed.stderr or "", policy.limits.max_stderr_bytes)
        if completed.returncode != 0:
            detail = _extract_failure_detail(completed)
            raise CodeExecutionError(
                f"MXC 沙箱异常退出，状态码 {completed.returncode}: {detail}"
            )

        # ---- 5) sidecar（不可信数据）与产物审计 ----
        sidecar = _load_sidecar(
            sidecar_path,
            run_id=request.run_id,
            limits=policy.limits,
        )
        if not sidecar["ok"]:
            detail = f"{sidecar['error_type']}: {sidecar['error']}"
            captured = "\n".join(
                text.strip() for text in (stdout_text, stderr_text) if text.strip()
            )
            if captured:
                detail += f"\n运行输出:\n{captured}"
            raise CodeExecutionError(detail)
        after = snapshot_artifacts(output_dir)
        changed = changed_artifacts(before, after)
        details = describe_artifacts(output_dir, changed, limits=policy.limits)
        expected = {
            normalize_relative_path(path) for path in request.expected_artifacts
        }
        missing_expected = sorted(expected - set(changed))
        if missing_expected:
            raise CodeExecutionError(
                "代码执行成功，但没有在当前运行目录生成承诺产物: "
                + ", ".join(missing_expected)
            )
        runtime = {
            "python_executable": str(executable),
            "backend": self.name,
            "isolation_tier": probe.tier,
            "source": request.environment_source,
            "name": request.environment_name,
        }
        emit_sandbox_event(
            "mxc.run.finish",
            run_id=request.run_id,
            tier=probe.tier,
            changed_artifacts=len(changed),
        )
        return {
            "stdout": stdout_text,
            "stderr": stderr_text,
            "result": sidecar["result"],
            "artifacts": sorted(after),
            "changed_artifacts": changed,
            "artifact_details": details,
            "runtime": runtime,
        }

    def run_command(
        self,
        request: CommandRunRequest,
        *,
        policy: SandboxPolicy,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        """终端命令执行在 Phase 2 接入（含 L0/L1/L2 审批模型演进）。"""
        raise NotImplementedError("终端命令的后端化将在 Phase 2 实施")

    def _cleanup_control_dir(self, control_dir: Path, *, run_id: str) -> None:
        """清理宿主控制目录；失败时记录告警而非抛出（保留受控证据）。"""
        try:
            shutil.rmtree(control_dir, ignore_errors=False)
        except OSError as exc:
            emit_sandbox_event(
                "mxc.cleanup.warning",
                run_id=run_id,
                control_dir=str(control_dir),
                error=str(exc),
            )


def _assert_control_isolation(control_dir: Path, output_dir: Path) -> None:
    """防御性检查：控制目录不得与授权的读写目录互相嵌套。"""
    resolved_control = control_dir.resolve(strict=False)
    resolved_output = output_dir.resolve(strict=False)
    if resolved_control == resolved_output:
        raise ValueError("控制目录不能与产物目录相同")
    if resolved_output in resolved_control.parents:
        raise ValueError("控制目录不能位于产物目录内部")
    if resolved_control in resolved_output.parents:
        raise ValueError("产物目录不能位于控制目录内部")


def _load_sidecar(
    sidecar_path: Path,
    *,
    run_id: str,
    limits: ExecutionLimits,
) -> dict:
    """读取并校验 sidecar 结果文件（全部字段按不可信数据对待）。

    Raises:
        SidecarProtocolError: 文件缺失、超限、畸形或字段类型非法。
    """
    if not sidecar_path.is_file():
        raise SidecarProtocolError(
            "沙箱未返回结果文件（可能被用户代码提前终止，如 os._exit）"
        )
    size = sidecar_path.stat().st_size
    if size > limits.max_result_bytes:
        raise SidecarProtocolError(
            f"结果文件超出体积上限（{size} > {limits.max_result_bytes} 字节）"
        )
    if size == 0:
        raise SidecarProtocolError("结果文件为空")
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SidecarProtocolError("结果文件不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise SidecarProtocolError("结果文件不是 JSON 对象")
    if payload.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        raise SidecarProtocolError(
            f"结果文件 schema_version 非法: {payload.get('schema_version')!r}"
        )
    # run_id 仅用于关联/过滤，不是签名或防伪凭证（§5.5 规则 1）
    if payload.get("run_id") != run_id:
        raise SidecarProtocolError("结果文件与当前任务不匹配")
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise SidecarProtocolError("结果文件缺少合法的 ok 字段")
    if ok:
        try:
            json.dumps(payload.get("result"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SidecarProtocolError("结果文件中的 result 不是合法 JSON 值") from exc
    else:
        if not isinstance(payload.get("error_type"), str):
            raise SidecarProtocolError("失败结果缺少 error_type 字段")
        if not isinstance(payload.get("error"), str):
            raise SidecarProtocolError("失败结果缺少 error 字段")
    return payload


def _extract_failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """从失败输出中提取可读原因（MXC 错误 JSON 优先，其次输出尾部）。"""
    stdout = (completed.stdout or "").strip()
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message:
                    return message[:1000]
        return stdout[-500:]
    stderr = (completed.stderr or "").strip()
    return stderr[-500:] if stderr else "无错误输出"


def _trim(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节上限截断文本（与既有 runner 的截断语义保持一致）。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n…输出已截断"
