"""连接 Agent Action、工具注册表和执行沙箱。"""

from __future__ import annotations

import time

from ..agent.loop import Action
from ..agent.state import Observation
from ..tools.registry import ToolNotFoundError, ToolRegistry
from .sandbox import SandboxError, SubprocessSandbox

__all__ = ["Harness"]


class Harness:
    """安全解析并执行一次 Agent 工具调用。"""

    def __init__(
        self,
        registry: ToolRegistry,
        sandbox: SubprocessSandbox | None = None,
    ) -> None:
        """初始化执行框架。

        Args:
            registry: 当前运行可用的工具注册表。
            sandbox: subprocess 沙箱；缺省时创建默认实例。
        """
        self._registry = registry
        self._sandbox = sandbox or SubprocessSandbox()

    def execute(self, action: Action) -> Observation:
        """执行一个 Action 并始终转换为标准 Observation。

        Args:
            action: 决策层输出且已通过 Pydantic 校验的工具调用。

        Returns:
            包含结果、错误与耗时的标准化 Observation。
        """
        started_at = time.monotonic()
        try:
            tool = self._registry.resolve(action.tool_name)
            result = self._sandbox.execute(tool, action.arguments)
        except (ToolNotFoundError, SandboxError) as exc:
            return Observation(
                tool_name=action.tool_name,
                success=False,
                error=str(exc),
                duration_ms=_elapsed_ms(started_at),
            )
        return Observation(
            tool_name=action.tool_name,
            success=True,
            result=result,
            duration_ms=_elapsed_ms(started_at),
        )


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))
