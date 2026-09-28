"""连接 Agent Action、工具注册表和执行沙箱。"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..agent.loop import Action
from ..agent.state import Observation
from ..tools.registry import ToolNotFoundError, ToolRegistry
from .execution_context import ExecutionContext
from ..cancellation import TaskCancelledError, raise_if_cancelled
from .sandbox import SandboxError, SubprocessSandbox

__all__ = ["Harness"]


class Harness:
    """安全解析并执行一次 Agent 工具调用。"""

    def __init__(
        self,
        registry: ToolRegistry,
        sandbox: SubprocessSandbox | None = None,
        command_approval: Callable[[Action], bool] | None = None,
        execution_context: ExecutionContext | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        """初始化执行框架。

        Args:
            registry: 当前运行可用的工具注册表。
            sandbox: subprocess 沙箱；缺省时创建默认实例。
            command_approval: 项目命令的一次性人工批准检查。
            execution_context: 当前 Run 可由工具自动继承的动态执行上下文。
            cancel_check: 用户请求终止时返回 True，将打断工具执行。
        """
        self._registry = registry
        self._sandbox = sandbox or SubprocessSandbox()
        self._command_approval = command_approval
        self._execution_context = execution_context
        self._cancel_check = cancel_check

    def execute(self, action: Action) -> Observation:
        """执行一个 Action 并始终转换为标准 Observation。

        Args:
            action: 决策层输出且已通过 Pydantic 校验的工具调用。

        Returns:
            包含结果、错误与耗时的标准化 Observation。
        """
        started_at = time.monotonic()
        raise_if_cancelled(self._cancel_check)
        if action.tool_name == "run_project_command" and (
            self._command_approval is None or not self._command_approval(action)
        ):
            return Observation(
                tool_name=action.tool_name,
                success=False,
                error="项目命令未获人工批准，未执行",
                duration_ms=_elapsed_ms(started_at),
            )
        effective_action = (
            self._execution_context.inherit(action)
            if self._execution_context is not None
            else action
        )
        try:
            tool = self._registry.resolve(effective_action.tool_name)
            context_override = (
                self._execution_context.tool_context()
                if self._execution_context is not None
                else None
            )
            if tool.run_in_process:
                context = dict(tool.context)
                if context_override:
                    context.update(context_override)
                result = tool.handler(effective_action.arguments, context)
            elif context_override is None:
                result = self._sandbox.execute(
                    tool, effective_action.arguments, cancel_check=self._cancel_check
                )
            else:
                result = self._sandbox.execute(
                    tool,
                    effective_action.arguments,
                    context_override=context_override,
                    cancel_check=self._cancel_check,
                )
        except TaskCancelledError:
            raise
        except (ToolNotFoundError, SandboxError, ValueError, RuntimeError) as exc:
            return Observation(
                tool_name=action.tool_name,
                success=False,
                error=str(exc),
                duration_ms=_elapsed_ms(started_at),
            )
        if action.tool_name == "run_project_command" and isinstance(result, dict):
            exit_code = result.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                details = str(result.get("stderr") or result.get("stdout") or "命令执行失败")
                return Observation(
                    tool_name=action.tool_name,
                    success=False,
                    result=result,
                    error=f"命令退出码 {exit_code}: {details[:1000]}",
                    duration_ms=_elapsed_ms(started_at),
                )
        try:
            self._update_execution_context(effective_action)
        except (OSError, ValueError) as exc:
            return Observation(
                tool_name=action.tool_name,
                success=False,
                result=result,
                error=f"执行环境状态更新失败: {exc}",
                duration_ms=_elapsed_ms(started_at),
            )
        return Observation(
            tool_name=action.tool_name,
            success=True,
            result=result,
            duration_ms=_elapsed_ms(started_at),
        )

    def _update_execution_context(self, action: Action) -> None:
        """工具成功后持久化显式选择或新创建的项目环境。"""
        if self._execution_context is None:
            return
        requested = action.arguments.get("python_environment")
        if action.tool_name in {"execute_python", "run_project_command"} and isinstance(
            requested,
            str,
        ):
            self._execution_context.select_python_environment(requested)
            return
        if action.tool_name == "run_project_command":
            self._execution_context.refresh_project_environment()


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))
