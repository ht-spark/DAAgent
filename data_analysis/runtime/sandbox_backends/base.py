"""沙箱后端契约：请求、策略、探测与后端协议（对齐设计方案 §4.2）。

本模块只定义数据契约，不包含执行逻辑；具体实现见 ``local_backend`` 与
``mxc_backend``，选择与路由见 ``service``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Mapping, Protocol

__all__ = [
    "IsolationRequirement",
    "ExecutionLimits",
    "SandboxPolicy",
    "PythonRunRequest",
    "CommandRunRequest",
    "BackendProbe",
    "SandboxBackend",
]

# 任务隔离要求：由服务端策略解析器决定，不接受 LLM/客户端输入降级
IsolationRequirement = Literal[
    "legacy_allowed",
    "mxc_required",
    "supported_mxc_required",
]


@dataclass(frozen=True)
class ExecutionLimits:
    """单次沙箱执行上限（默认值对齐现有 runner 的能力边界）。"""

    # 与 tools/code_tools.py 的既有默认超时保持一致（约 2 小时）
    timeout_seconds: float = 7_100.0
    # 与 SubprocessSandbox 的输出上限保持一致
    max_stdout_bytes: int = 2_000_000
    max_stderr_bytes: int = 2_000_000
    # sidecar 结果文件上限（设计方案 §5.5：默认 1 MiB）
    max_result_bytes: int = 1_048_576
    # 产物枚举上限（防御文件洪泛；默认宽松避免误伤正常分析任务）
    max_artifact_files: int = 1_000
    max_artifact_bytes: int = 1_073_741_824


@dataclass(frozen=True)
class SandboxPolicy:
    """服务端解析出的执行策略。

    该对象只允许由 ``policy.PolicyResolver``（受信任服务端代码）构造，
    不作为 LLM 工具参数公开；调用方不得通过工具参数降低隔离要求。
    """

    required_isolation: IsolationRequirement = "legacy_allowed"
    # 仅可由服务端可信配置赋值；控制 auto 模式在探测失败时的降级许可
    allow_local_fallback: bool = False
    # Phase 1 默认断网；联网需求须走升级审批（设计方案 §6.4）
    allow_network: bool = False
    # 默认禁止子进程；后端无法强制时须拒绝或显式声明（设计方案 §5.7）
    allow_child_processes: bool = False
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)


@dataclass(frozen=True)
class PythonRunRequest:
    """一次动态 Python 代码执行请求。"""

    run_id: str
    code: str
    data_paths: tuple[Path, ...]
    output_dir: Path
    workspace_root: Path | None
    python_executable: Path
    environment_source: str = "host"
    environment_name: str = "MMAgent"
    expected_artifacts: tuple[str, ...] = ()
    skill_assets: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 7_100.0


@dataclass(frozen=True)
class CommandRunRequest:
    """一次终端命令执行请求（Phase 2 接入，当前保留契约）。"""

    run_id: str
    command: str
    workspace_root: Path
    shell: str = "powershell"
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class BackendProbe:
    """后端可用性探测结果。"""

    available: bool
    backend: str
    tier: str = "unknown"
    reason: str = ""
    warnings: tuple[str, ...] = ()


class SandboxBackend(Protocol):
    """沙箱后端协议。

    实现必须保证：
    - ``run_python`` 成功时返回与既有响应一致的字典
      （``stdout``/``stderr``/``result``/``artifacts``/``changed_artifacts``/
      ``artifact_details``/``runtime``）；
    - 失败时抛出 ``CodeExecutionError``（业务失败）或 ``SandboxBackendError``
      （基础设施失败），由上层转换为标准 Observation；
    - ``policy`` 参数为受信任策略，实现不得允许绕过。
    """

    name: str

    def probe(self) -> BackendProbe:
        """探测本机可用性（实现内部应缓存结果）。"""
        ...

    def run_python(
        self,
        request: PythonRunRequest,
        *,
        policy: SandboxPolicy,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        """在沙箱中执行动态 Python 代码。"""
        ...

    def run_command(
        self,
        request: CommandRunRequest,
        *,
        policy: SandboxPolicy,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        """在沙箱中执行终端命令。"""
        ...
