"""连接 Agent Action、工具注册表和执行沙箱。"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import nullcontext

from ..agent.loop import Action
from ..agent.state import Observation
from ..observability.trace import RunTrace
from ..tools.registry import ToolNotFoundError, ToolRegistry
from ..tools.terminal_tools import TERMINAL_TOOL_NAMES
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
        terminal_approval: Callable[[Action], bool] | None = None,
        execution_context: ExecutionContext | None = None,
        cancel_check: Callable[[], bool] | None = None,
        trace: RunTrace | None = None,
        max_parallel_tools: int = 4,
    ) -> None:
        """初始化执行框架。

        Args:
            registry: 当前运行可用的工具注册表。
            sandbox: subprocess 沙箱；缺省时创建默认实例。
            terminal_approval: 终端调用的一次性人工批准检查。
            execution_context: 当前 Run 可由工具自动继承的动态执行上下文。
            cancel_check: 用户请求终止时返回 True，将打断工具执行。
            trace: 本次运行的可选本地追踪容器。
            max_parallel_tools: 默认 4 个只读工具并发，范围 1～16；设为 1 完全串行。
                调大增加吞吐和内存/外部服务压力，不会放开有写入副作用的工具。
        """
        self._registry = registry
        self._sandbox = sandbox or SubprocessSandbox()
        self._terminal_approval = terminal_approval
        self._execution_context = execution_context
        self._cancel_check = cancel_check
        self._trace = trace
        if type(max_parallel_tools) is not int or not 1 <= max_parallel_tools <= 16:
            raise ValueError("max_parallel_tools 必须为 1～16 的整数")
        self._max_parallel_tools = max_parallel_tools

    def can_parallel(self, actions: Sequence[Action]) -> bool:
        """整批仅包含显式许可的只读工具时才允许并行。"""
        if len(actions) < 2 or self._max_parallel_tools == 1:
            return False
        for action in actions:
            if action.tool_name in TERMINAL_TOOL_NAMES:
                return False
            try:
                spec = self._registry.resolve(action.tool_name).spec
            except ToolNotFoundError:
                return False
            if spec.mutating or not spec.parallel_safe:
                return False
        return True

    def execute_many(self, actions: Sequence[Action]) -> list[Observation]:
        """有界并行执行安全只读批次，返回顺序始终与输入一致。

        Args:
            actions: 已校验且参数互不依赖的调用；含写操作时整批串行。

        Returns:
            与 actions 一一对应的观察；取消时停止等待并撤销尚未启动的调用。
        """
        if not self.can_parallel(actions):
            return [self.execute(action) for action in actions]
        raise_if_cancelled(self._cancel_check)
        parent_id = self._trace.current_span_id if self._trace else None

        def execute_branch(action: Action) -> Observation:
            scope = self._trace.parent_scope(parent_id) if self._trace else nullcontext()
            with scope:
                return self.execute(action)

        executor = ThreadPoolExecutor(max_workers=self._max_parallel_tools, thread_name_prefix="agent-tools")
        futures = [executor.submit(execute_branch, action) for action in actions]
        try:
            results = []
            for future in futures:
                while True:
                    raise_if_cancelled(self._cancel_check)
                    try:
                        # 每 100ms 检查一次取消，避免等待排在前面的慢工具阻塞取消。
                        results.append(future.result(timeout=0.1))
                        break
                    except FutureTimeoutError:
                        if future.done():
                            raise
            return results
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def rejected(self, action: Action, error: str) -> Observation:
        """为重复拦截等未执行调用生成可关联、可追踪的失败观察。"""
        span = self._trace.begin("tool", action.tool_name) if self._trace else None
        if self._trace:
            self._trace.end(span, status="error", error_type="RepeatedToolCall")
        return Observation(
            tool_name=action.tool_name, call_id=action.call_id, success=False, error=error,
            span_id=span.span_id if span else None,
        )

    def execute(self, action: Action) -> Observation:
        """执行一个 Action 并始终转换为标准 Observation。

        Args:
            action: 决策层输出且已通过 Pydantic 校验的工具调用。

        Returns:
            包含结果、错误与耗时的标准化 Observation。
        """
        if self._trace is None:
            return self._execute(action).model_copy(update={"call_id": action.call_id})
        with self._trace.span("tool", action.tool_name) as span:
            observation = self._execute(action)
            self._trace.end(
                span, status="ok" if observation.success else "error",
                error_type=None if observation.success else "ToolExecutionError",
                duration_ms=observation.duration_ms,
            )
            return observation.model_copy(update={
                "span_id": span.span_id if span else None,
                "call_id": action.call_id,
                "input_tokens": span.input_tokens if span else None,
                "output_tokens": span.output_tokens if span else None,
                "cost_estimate": span.cost_estimate if span else None,
            })

    def _execute(self, action: Action) -> Observation:
        """保留原有执行语义，由外层统一采集所有返回分支。"""
        started_at = time.monotonic()
        raise_if_cancelled(self._cancel_check)
        if action.tool_name in TERMINAL_TOOL_NAMES and (
            self._terminal_approval is None or not self._terminal_approval(action)
        ):
            return Observation(
                tool_name=action.tool_name,
                success=False,
                error="终端命令未获人工批准，未执行",
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
        if action.tool_name in TERMINAL_TOOL_NAMES and isinstance(result, dict):
            exit_code = result.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                details = str(result.get("output") or "命令执行失败")
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
        if action.tool_name == "execute_python" and isinstance(requested, str):
            self._execution_context.select_python_environment(requested)


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))
