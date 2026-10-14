"""SandboxService：后端选择、路由与降级裁决（设计方案 §6.2 路由矩阵）。

路由矩阵（简化实现，语义与设计方案一致）：

| 配置/条件 | 行为 |
|---|---|
| ``local`` + ``legacy_allowed`` | 使用 local 后端 |
| ``local`` + 要求 MXC 隔离 | **拒绝执行**（不伪装已隔离） |
| ``mxc`` + probe 可用 | 使用 MXC |
| ``mxc`` + probe 失败 | **直接报错，不降级**（fail-closed） |
| ``auto`` + probe 可用 | 使用 MXC |
| ``auto`` + probe 失败 + 显式许可降级 + 非 production 要求 | 回退 local 并记录告警 |
| ``auto`` + 其他失败 | **拒绝执行** |

关键原则：运行级 ``backend`` 标识只允许来自服务端受信任注入
（``ExecutionContext``），不接受 LLM/工具参数；不可信任务零静默降级。
"""

from __future__ import annotations

from typing import Callable, Mapping

from .base import CommandRunRequest, PythonRunRequest, SandboxBackend, SandboxPolicy
from .errors import SandboxPolicyError, SandboxUnavailableError
from .local_backend import LocalBackend
from .mxc_backend import MxcBackend
from .policy import SandboxSettings, load_settings, resolve_policy
from .trace import emit_sandbox_event

__all__ = [
    "SandboxService",
    "get_sandbox_service",
    "reset_sandbox_service",
    "VALID_BACKEND_NAMES",
]

VALID_BACKEND_NAMES = frozenset({"local", "mxc", "auto"})


class SandboxService:
    """按受信任策略选择并调度沙箱后端。"""

    def __init__(
        self,
        settings: SandboxSettings,
        *,
        backends: Mapping[str, SandboxBackend] | None = None,
    ) -> None:
        """初始化服务。

        Args:
            settings: 服务端沙箱配置。
            backends: 可选的后端映射（测试注入用）；缺省时构建 local + mxc。
        """
        self._settings = settings
        self._policy = resolve_policy(settings)
        if backends is not None:
            self._backends: dict[str, SandboxBackend] = dict(backends)
        else:
            self._backends = {
                "local": LocalBackend(),
                "mxc": MxcBackend(
                    settings.mxc_executable or "",
                    require_supported_host=settings.require_supported_host,
                ),
            }

    @property
    def policy(self) -> SandboxPolicy:
        """当前服务解析出的受信任策略。"""
        return self._policy

    @property
    def settings(self) -> SandboxSettings:
        """当前服务配置。"""
        return self._settings

    def run_python(
        self,
        request: PythonRunRequest,
        *,
        backend: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        """按路由矩阵选择后端并执行一次 Python 代码。

        Args:
            request: 执行请求。
            backend: 运行级后端标识（服务端注入）；为空时使用进程级配置。
            cancel_check: 取消检查回调。

        Raises:
            SandboxPolicyError: 后端标识非法或策略不允许执行。
            SandboxUnavailableError: MXC 不可用且不允许降级。
        """
        name = self._decide_backend(backend)
        implementation = self._backends[name]
        emit_sandbox_event(
            "service.dispatch",
            run_id=request.run_id,
            requested=backend or self._settings.backend,
            effective=name,
        )
        return implementation.run_python(
            request,
            policy=self._policy,
            cancel_check=cancel_check,
        )

    def run_command(
        self,
        request: CommandRunRequest,
        *,
        backend: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        """按路由矩阵选择后端并执行一次终端命令（Phase 2 接线）。"""
        name = self._decide_backend(backend)
        implementation = self._backends[name]
        return implementation.run_command(
            request,
            policy=self._policy,
            cancel_check=cancel_check,
        )

    def _decide_backend(self, requested: str | None) -> str:
        """按路由矩阵裁决实际执行后端；不可满足时 fail-closed。"""
        configured = (requested or self._settings.backend).strip().casefold()
        if configured not in VALID_BACKEND_NAMES:
            raise SandboxPolicyError(f"未知的沙箱后端标识: {configured or '<空>'}")
        required = self._policy.required_isolation
        if configured == "local":
            if required != "legacy_allowed":
                raise SandboxPolicyError(
                    "当前策略要求 MXC 隔离，但后端配置为 local；已拒绝执行"
                )
            return "local"
        probe = self._backends["mxc"].probe()
        if probe.available:
            return "mxc"
        # probe 失败：生产级要求绝不降级；仅 auto + 显式许可 + 非 production 才回退
        if (
            configured == "auto"
            and self._policy.allow_local_fallback
            and required != "supported_mxc_required"
        ):
            emit_sandbox_event(
                "service.fallback",
                reason=probe.reason,
                effective="local",
            )
            return "local"
        raise SandboxUnavailableError(
            f"MXC 后端不可用且当前配置不允许降级: {probe.reason}"
        )


_default_service: SandboxService | None = None


def get_sandbox_service() -> SandboxService:
    """返回进程级默认服务（首次调用时从环境变量构建配置）。"""
    global _default_service
    if _default_service is None:
        _default_service = SandboxService(load_settings())
    return _default_service


def reset_sandbox_service() -> None:
    """重置进程级默认服务（配置变更或测试使用）。"""
    global _default_service
    _default_service = None
