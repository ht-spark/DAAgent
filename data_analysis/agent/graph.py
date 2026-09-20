"""串联 Agent 决策、工具执行和状态更新。"""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict

from .loop import Action, AgentLoop, Finish
from .planner import Planner
from .state import AgentState, AnalysisPlan, Observation, TodoItem
from ..runtime.failure_tracker import (
    FailureTracker,
    RepeatedToolFailureError,
)
from ..runtime.harness import Harness

__all__ = [
    "GraphEvent",
    "PlanGraphEvent",
    "PlanCreated",
    "TodoStarted",
    "TodoCompleted",
    "TodoFailed",
    "iter_graph",
    "iter_plan_graph",
    "run_graph",
    "run_plan_graph",
]

GraphEvent = Action | Observation | Finish


class PlanCreated(BaseModel):
    """Planner 已生成顶层 Todo List。"""

    model_config = ConfigDict(frozen=True)
    plan: AnalysisPlan


class TodoStarted(BaseModel):
    """顶层调度器开始执行一个 Todo。"""

    model_config = ConfigDict(frozen=True)
    todo_id: str
    objective: str


class TodoCompleted(BaseModel):
    """一个 Todo 已由内部 ReAct 完成。"""

    model_config = ConfigDict(frozen=True)
    todo_id: str
    result_summary: str


class TodoFailed(BaseModel):
    """一个 Todo 因重复工具失败触发熔断。"""

    model_config = ConfigDict(frozen=True)
    todo_id: str
    error: str


PlanGraphEvent = GraphEvent | PlanCreated | TodoStarted | TodoCompleted | TodoFailed


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
    failures = FailureTracker()
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
        circuit = failures.record(decision, observation)
        if circuit is not None:
            raise RepeatedToolFailureError(_failure_summary(circuit))


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


def iter_plan_graph(
    state: AgentState,
    planner: Planner,
    loop: AgentLoop,
    harness: Harness,
    *,
    failure_threshold: int = 3,
    max_replans: int = 2,
) -> Iterator[PlanGraphEvent]:
    """运行顶层 Plan-and-Execute，并复用原有 ReAct 完成每个 Todo。"""
    plan = planner.create_plan(state)
    state.analysis_plan = plan
    replan_count = 0
    yield PlanCreated(plan=plan)

    while True:
        todo = _next_todo(plan)
        if todo is None:
            break
        todo.status = "in_progress"
        state.current_todo_id = todo.id
        yield TodoStarted(todo_id=todo.id, objective=todo.objective)

        todo_state = _todo_state(state, todo)
        failures = FailureTracker(threshold=failure_threshold)
        while True:
            decision = loop.step(todo_state)
            if isinstance(decision, Finish):
                todo.status = "completed"
                todo.result_summary = decision.answer
                state.current_todo_id = None
                yield TodoCompleted(
                    todo_id=todo.id,
                    result_summary=decision.answer,
                )
                break
            yield decision
            observation = harness.execute(decision)
            todo_state.record_observation(observation)
            state.record_observation(observation)
            yield observation
            circuit = failures.record(decision, observation)
            if circuit is None:
                continue
            failure_summary = _failure_summary(circuit)

            todo.status = "failed"
            todo.result_summary = failure_summary
            state.current_todo_id = None
            yield TodoFailed(todo_id=todo.id, error=failure_summary)
            if replan_count >= max_replans:
                raise RepeatedToolFailureError(
                    f"恢复规划次数已达上限 {max_replans}；{failure_summary}"
                )
            recovery = planner.create_recovery_plan(
                state,
                plan,
                failed_todo_id=todo.id,
                failure_summary=failure_summary,
            )
            replan_count += 1
            plan = _merge_recovery_plan(plan, recovery, replan_count)
            state.analysis_plan = plan
            yield PlanCreated(plan=plan)
            break

    final_state = _final_state(state, plan)
    final_failures = FailureTracker(threshold=failure_threshold)
    while True:
        decision = loop.step(final_state)
        if isinstance(decision, Finish):
            state.append_message("user", state.user_query)
            state.append_message("assistant", decision.answer)
            yield decision
            return
        yield decision
        observation = harness.execute(decision)
        final_state.record_observation(observation)
        state.record_observation(observation)
        yield observation
        circuit = final_failures.record(decision, observation)
        if circuit is not None:
            raise RepeatedToolFailureError(_failure_summary(circuit))


def run_plan_graph(
    state: AgentState,
    planner: Planner,
    loop: AgentLoop,
    harness: Harness,
) -> Finish:
    """运行 Plan-and-Execute，直到最终汇总回答产生。"""
    for event in iter_plan_graph(state, planner, loop, harness):
        if isinstance(event, Finish):
            return event
    raise RuntimeError("Plan-and-Execute 图未产生最终回答")


def _failure_summary(circuit: FailureCircuitOpen) -> str:
    """生成可交给 Planner 和用户的稳定熔断摘要。"""
    return (
        f"工具 {circuit.tool_name} 以相同参数和错误累计失败 "
        f"{circuit.count} 次：{circuit.error}"
    )


def _merge_recovery_plan(
    previous: AnalysisPlan,
    recovery: AnalysisPlan,
    revision: int,
) -> AnalysisPlan:
    """保留已结束 Todo，并为恢复计划分配不会冲突的新标识。"""
    preserved = [
        todo.model_copy(deep=True)
        for todo in previous.todos
        if todo.status in {"completed", "failed"}
    ]
    prefix = f"recovery_{revision}_"
    identifier_map = {todo.id: prefix + todo.id for todo in recovery.todos}
    revised = [
        todo.model_copy(update={
            "id": identifier_map[todo.id],
            "depends_on": [identifier_map[item] for item in todo.depends_on],
        }, deep=True)
        for todo in recovery.todos
    ]
    return AnalysisPlan(
        objective=previous.objective,
        reasoning_summary=recovery.reasoning_summary,
        todos=[*preserved, *revised],
    )


def _next_todo(plan: AnalysisPlan) -> TodoItem | None:
    """按计划顺序选择依赖均已完成的下一个 Todo。"""
    completed = {todo.id for todo in plan.todos if todo.status == "completed"}
    pending = [todo for todo in plan.todos if todo.status == "pending"]
    for todo in pending:
        if set(todo.depends_on) <= completed:
            return todo
    if pending:
        blocked = ", ".join(todo.id for todo in pending)
        raise RuntimeError(f"Todo 依赖未满足，计划无法继续: {blocked}")
    return None


def _todo_state(state: AgentState, todo: TodoItem) -> AgentState:
    """为一个 Todo 创建隔离的内部 ReAct 状态。"""
    completed = [
        {"id": item.id, "objective": item.objective, "result": item.result_summary}
        for item in state.analysis_plan.todos
        if item.status == "completed"
    ] if state.analysis_plan else []
    query = (
        f"原始用户目标：{state.user_query}\n\n"
        f"当前只执行 Todo {todo.id}：{todo.objective}\n"
        f"预期输出：{todo.expected_outputs}\n"
        f"已完成 Todo：{completed}\n\n"
        "请使用现有 ReAct 工具循环完成当前 Todo；信息充分后返回 finish，"
        "finish.answer 应是可供后续 Todo 和最终汇总使用的结果摘要。"
    )
    return AgentState(
        user_query=query,
        memory_summary=state.memory_summary,
        data_paths=list(state.data_paths),
        output_dir=state.output_dir,
        workspace_root=state.workspace_root,
        project_id=state.project_id,
        available_artifacts=list(state.available_artifacts),
        messages=list(state.messages),
        analysis_plan=state.analysis_plan,
        current_todo_id=todo.id,
    )


def _final_state(state: AgentState, plan: AnalysisPlan) -> AgentState:
    """构造最终汇总使用的独立 ReAct 状态。"""
    results = [
        {
            "id": todo.id,
            "objective": todo.objective,
            "result_summary": todo.result_summary,
        }
        for todo in plan.todos
    ]
    query = (
        f"原始用户目标：{state.user_query}\n\n"
        f"执行计划结果：{results}\n\n"
        "请综合所有 Todo 结果回答原始用户问题。不得声称未在结果中出现的事实；"
        "如确需补充核验，可以继续使用现有工具，否则直接返回 finish。"
    )
    return AgentState(
        user_query=query,
        memory_summary=state.memory_summary,
        data_paths=list(state.data_paths),
        output_dir=state.output_dir,
        workspace_root=state.workspace_root,
        project_id=state.project_id,
        available_artifacts=list(state.available_artifacts),
        messages=list(state.messages),
        analysis_plan=plan,
    )
