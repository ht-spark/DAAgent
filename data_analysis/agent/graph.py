"""串联 Agent 决策、工具执行和状态更新。"""

from __future__ import annotations

from collections.abc import Iterator

from .loop import Action, AgentLoop, Finish
from .state import AgentState, Observation
from ..runtime.harness import Harness

__all__ = ["GraphEvent", "iter_graph", "run_graph"]

GraphEvent = Action | Observation | Finish


def iter_graph(
    state: AgentState,
    loop: AgentLoop,
    harness: Harness,
) -> Iterator[GraphEvent]:
    """逐事件运行思考、执行、观察循环。

    决策和观察会在各自产生后立即交给调用方，适合 HTTP 流式接口展示
    Agent 运行轨迹。最终回答产生时会先写入状态，再作为最后一个事件返回。

    Args:
        state: 本次运行的可变 Agent 状态。
        loop: 只负责产生下一步决策的 Agent Loop。
        harness: 负责解析注册工具并执行 Action 的 Harness。

    Yields:
        按发生顺序产生的 Action、Observation 或 Finish。

    Raises:
        AgentLoopError: LLM 决策无效或超过最大迭代次数。
    """
    while True:
        decision = loop.step(state)
        if isinstance(decision, Finish):
            state.append_message("user", state.user_query)
            state.append_message("assistant", decision.answer)
            yield decision
            return

        yield decision
        observation = harness.execute(decision)
        state.record_observation(observation)
        yield observation


def run_graph(
    state: AgentState,
    loop: AgentLoop,
    harness: Harness,
) -> Finish:
    """运行思考—执行—观察循环，直到 Agent 返回最终回答。

    每次 ``Action`` 由 Harness 执行，其 ``Observation`` 会写回状态并供
    下一轮决策读取。收到 ``Finish`` 后，本轮问答会追加到消息历史。

    Args:
        state: 本次运行的可变 Agent 状态。
        loop: 只负责产生下一步决策的 Agent Loop。
        harness: 负责解析注册工具并执行 Action 的 Harness。

    Returns:
        LLM 产生且已经通过校验的最终回答。

    Raises:
        AgentLoopError: LLM 决策无效或超过最大迭代次数。
    """
    for event in iter_graph(state, loop, harness):
        if isinstance(event, Finish):
            return event
    raise RuntimeError("Agent 图未产生最终回答")
