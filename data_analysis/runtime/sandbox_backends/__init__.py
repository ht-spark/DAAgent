"""沙箱后端包：local 与 MXC 双后端、策略解析与服务路由。

对外只暴露契约（``base``）、错误分类（``errors``）、策略（``policy``）
与服务入口（``service``）；具体后端实现保持内部可见性。
"""

from .base import (
    BackendProbe,
    CommandRunRequest,
    ExecutionLimits,
    IsolationRequirement,
    PythonRunRequest,
    SandboxBackend,
    SandboxPolicy,
)
from .errors import (
    CodeExecutionError,
    SandboxBackendError,
    SandboxPolicyError,
    SandboxUnavailableError,
    SidecarProtocolError,
)
from .policy import SandboxSettings, load_settings, resolve_policy
from .service import SandboxService, get_sandbox_service, reset_sandbox_service

__all__ = [
    "BackendProbe",
    "CommandRunRequest",
    "CodeExecutionError",
    "ExecutionLimits",
    "IsolationRequirement",
    "PythonRunRequest",
    "SandboxBackend",
    "SandboxBackendError",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxService",
    "SandboxSettings",
    "SandboxUnavailableError",
    "SidecarProtocolError",
    "get_sandbox_service",
    "load_settings",
    "reset_sandbox_service",
    "resolve_policy",
]
