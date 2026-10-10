"""串联 Agent 决策、工具执行和状态更新。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from pydantic import BaseModel, ConfigDict

from .loop import Action, AgentLoop, Finish
from .planner import Planner
from .state import AgentState, AnalysisPlan, Observation, TodoItem, clip_text
from ..skills.models import SkillAction, SkillObservation
from ..skills.runtime import SkillRuntime
from ..runtime.failure_tracker import (
    FailureTracker,
    RepeatedToolFailureError,
)
from ..runtime.repeat_guard import RepeatGuard
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

GraphEvent = Action | SkillAction | Observation | SkillObservation | Finish

# Todo 结果进入后续 prompt 的字符预算，避免长回答被反复整段复制
MAX_RESULT_SUMMARY_CHARS = 2_000          # 单个 Todo 结果摘要的字符上限
MAX_PLAN_RESULTS_CHARS = 16_000           # 单次 prompt 内全部已完成结果的总预算


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
    skill_runtime: SkillRuntime | None = None,
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
    repeats = RepeatGuard()
    skill_repeats: set[str] = set()
    while True:
        decision = loop.step(state)
        if isinstance(decision, Finish):
            state.append_message("user", state.user_query)
            state.append_message("assistant", decision.answer)
            yield decision
            return

        yield decision
        if isinstance(decision, SkillAction):
            observation = _execute_skill_once(decision, skill_runtime, skill_repeats)
            state.record_skill_observation(observation)
        else:
            observation = _execute_once(decision, harness, repeats)
            state.record_observation(observation)
        yield observation
        if isinstance(decision, Action) and isinstance(observation, Observation):
            circuit = failures.record(decision, observation)
            if circuit is not None:
                raise RepeatedToolFailureError(_failure_summary(circuit))


def run_graph(
    state: AgentState,
    loop: AgentLoop,
    harness: Harness,
    skill_runtime: SkillRuntime | None = None,
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
    for event in iter_graph(state, loop, harness, skill_runtime):
        if isinstance(event, Finish):
            return event
    raise RuntimeError("Agent 图未产生最终回答")


def iter_plan_graph(
    state: AgentState,
    planner: Planner,
    loop: AgentLoop,
    harness: Harness,
    *,
    skill_runtime: SkillRuntime | None = None,
    failure_threshold: int = 3,
    max_replans: int = 2,
    max_todo_steps: int = 30,
) -> Iterator[PlanGraphEvent]:
    """运行顶层 Plan-and-Execute，并复用原有 ReAct 完成每个 Todo。

    Args:
        state: 本次运行的可变 Agent 状态。
        planner: 顶层规划器。
        loop: 只产生决策的 ReAct 循环。
        harness: 执行工具并返回标准化观察。
        failure_threshold: 相同失败达到该次数即熔断当前 Todo。
        max_replans: 允许的恢复规划次数上限。
        max_todo_steps: 单个 Todo 允许的决策轮数上限；达到后按 Todo 失败处理，
            交给恢复规划换策略，避免一个 Todo 吃掉整轮预算。
    """
    if max_todo_steps < 1:
        raise ValueError("max_todo_steps 必须大于 0")
    plan = planner.create_plan(state)
    if isinstance(plan, Finish):
        state.append_message("user", state.user_query)
        state.append_message("assistant", plan.answer)
        yield plan
        return
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
        repeats = RepeatGuard()
        skill_repeats: set[str] = set()
        failure_summary: str | None = None
        while True:
            if todo_state.iteration >= max_todo_steps:
                failure_summary = (
                    f"Todo {todo.id} 达到单个 Todo 的决策轮数上限 "
                    f"{max_todo_steps}：{_repeat_hint(repeats)}"
                )
                break
            decision = loop.step(todo_state)
            if isinstance(decision, Finish):
                todo.status = "completed"
                todo.result_summary = decision.answer
                state.current_todo_id = None
                state.work_trajectory.add_decision(f"{todo.id}: {decision.answer}")
                yield TodoCompleted(
                    todo_id=todo.id,
                    result_summary=decision.answer,
                )
                break
            yield decision
            if isinstance(decision, SkillAction):
                observation = _execute_skill_once(decision, skill_runtime, skill_repeats)
                todo_state.record_skill_observation(observation)
                state.absorb_skill_observation(observation)
            else:
                observation = _execute_once(decision, harness, repeats)
                todo_state.record_observation(observation)
                # 运行级状态只聚合产物与轨迹，不重复留存结果、也不重复计入预算
                state.absorb_observation(observation)
            yield observation
            if isinstance(decision, SkillAction):
                continue
            circuit = failures.record(decision, observation)
            if circuit is None:
                continue
            failure_summary = _failure_summary(circuit)
            break

        if failure_summary is None:
            continue

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

    final_state = _final_state(state, plan)
    final_failures = FailureTracker(threshold=failure_threshold)
    final_repeats = RepeatGuard()
    final_skill_repeats: set[str] = set()
    while True:
        decision = loop.step(final_state)
        if isinstance(decision, Finish):
            state.append_message("user", state.user_query)
            state.append_message("assistant", decision.answer)
            yield decision
            return
        yield decision
        if isinstance(decision, SkillAction):
            observation = _execute_skill_once(decision, skill_runtime, final_skill_repeats)
            final_state.record_skill_observation(observation)
            state.absorb_skill_observation(observation)
        else:
            observation = _execute_once(decision, harness, final_repeats)
            final_state.record_observation(observation)
            state.absorb_observation(observation)
        yield observation
        if isinstance(decision, Action) and isinstance(observation, Observation):
            circuit = final_failures.record(decision, observation)
            if circuit is not None:
                raise RepeatedToolFailureError(_failure_summary(circuit))


def _execute_skill_once(
    decision: SkillAction,
    runtime: SkillRuntime | None,
    fingerprints: set[str],
) -> SkillObservation:
    """执行一次 Skill 操作，并拦截同阶段内完全相同的重复请求。"""
    fingerprint = json.dumps(
        decision.model_dump(mode="json", exclude={"thought", "reasoning"}),
        ensure_ascii=False,
        sort_keys=True,
    )
    if fingerprint in fingerprints:
        return SkillObservation(
            operation=decision.operation,
            skill_id=decision.skill_id,
            resource_id=decision.resource_id,
            entrypoint_id=decision.entrypoint_id,
            success=False,
            error="相同 Skill 操作已执行过，请复用已有结果或调整参数",
        )
    fingerprints.add(fingerprint)
    if runtime is None:
        return SkillObservation(
            operation=decision.operation,
            skill_id=decision.skill_id,
            resource_id=decision.resource_id,
            entrypoint_id=decision.entrypoint_id,
            success=False,
            error="当前运行未启用 SkillRuntime",
        )
    return runtime.execute(decision)


def _execute_once(
    decision: Action,
    harness: Harness,
    repeats: RepeatGuard,
) -> Observation:
    """执行一次决策；参数完全相同的重复调用直接拦截，不再真正执行。

    重复调用不会带来新信息，却要付出工具耗时与上下文成本，因此在执行前
    拦截并返回一条带说明的观察，让模型改用已有结果或更换参数。
    """
    notice = repeats.check(decision)
    if notice is not None:
        return Observation(
            tool_name=decision.tool_name,
            success=False,
            error=notice.message(),
        )
    observation = harness.execute(decision)
    repeats.record(decision, observation)
    return observation


def _repeat_hint(guards: RepeatGuard) -> str:
    """汇总本次 Todo 内被拦截的重复调用，供恢复规划换策略。"""
    blocked = guards.blocked_count()
    if blocked == 0:
        return "该 Todo 未出现重复调用，请拆分目标或减少不必要的核验步骤。"
    return (
        f"其中 {blocked} 次为参数完全相同的重复调用（已被运行时拦截）："
        f"{guards.blocked_summary()}。请更换工具、参数或直接复用已有结果。"
    )


def run_plan_graph(
    state: AgentState,
    planner: Planner,
    loop: AgentLoop,
    harness: Harness,
    skill_runtime: SkillRuntime | None = None,
) -> Finish:
    """运行 Plan-and-Execute，直到最终汇总回答产生。"""
    for event in iter_plan_graph(
        state, planner, loop, harness, skill_runtime=skill_runtime
    ):
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
    """保留已结束 Todo，并为恢复计划分配不会冲突的新标识。

    恢复计划中的依赖可能引用本计划内的新标识、已保留的历史标识或未知标识；
    未知依赖会被丢弃，避免重规划时因越界引用直接失败。
    """
    preserved = [
        todo.model_copy(deep=True)
        for todo in previous.todos
        if todo.status in {"completed", "failed"}
    ]
    prefix = f"recovery_{revision}_"
    identifier_map = {todo.id: prefix + todo.id for todo in recovery.todos}
    known_ids = {todo.id for todo in preserved}

    def _revised_dependencies(todo: TodoItem) -> list[str]:
        mapped: list[str] = []
        for item in todo.depends_on:
            if item in identifier_map:
                mapped.append(identifier_map[item])
            elif item in known_ids:
                mapped.append(item)
        return mapped

    revised = [
        todo.model_copy(update={
            "id": identifier_map[todo.id],
            "depends_on": _revised_dependencies(todo),
        }, deep=True)
        for todo in recovery.todos
    ]
    return AnalysisPlan(
        objective=previous.objective,
        reasoning_summary=recovery.reasoning_summary,
        model_reasoning=recovery.model_reasoning,
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


def _project_results(
    todos: Iterable[TodoItem],
    *,
    budget: int = MAX_PLAN_RESULTS_CHARS,
) -> list[dict[str, str]]:
    """按预算投影 Todo 结果摘要，最近完成的优先保留完整内容。

    Args:
        todos: 待投影的 Todo（通常只传已完成项）。
        budget: 全部结果摘要的总字符预算。

    Returns:
        含 id、objective 与受限 result_summary 的结果列表（保持原顺序）。
    """
    items: list[dict[str, str]] = []
    used = 0
    for todo in reversed(list(todos)):
        text = clip_text(todo.result_summary or "", MAX_RESULT_SUMMARY_CHARS)
        if items and used + len(text) > budget:
            text = "（结果过长已省略，请参考已有产物与会话摘要）"
        items.append(
            {
                "id": todo.id,
                "objective": todo.objective,
                "result_summary": text,
            }
        )
        used += len(text)
    items.reverse()
    return items


def _todo_state(state: AgentState, todo: TodoItem) -> AgentState:
    """为一个 Todo 创建隔离的内部 ReAct 状态。"""
    completed = _project_results(
        [item for item in state.analysis_plan.todos if item.status == "completed"]
    ) if state.analysis_plan else []
    query = (
        f"原始用户目标：{state.user_query}\n\n"
        f"当前只执行 Todo {todo.id}：{todo.objective}\n"
        f"预期输出：{json.dumps(todo.expected_outputs, ensure_ascii=False)}\n"
        f"已完成 Todo：{json.dumps(completed, ensure_ascii=False)}\n\n"
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
        work_trajectory=state.work_trajectory.model_copy(deep=True),
        run_budget=state.run_budget,
        expert_context=state.expert_context,
        skill_context=state.skill_context,
        skill_observations=list(state.skill_observations),
    )


def _final_state(state: AgentState, plan: AnalysisPlan) -> AgentState:
    """构造最终汇总使用的独立 ReAct 状态。"""
    results = _project_results(plan.todos)
    query = (
        f"原始用户目标：{state.user_query}\n\n"
        f"执行计划结果：{json.dumps(results, ensure_ascii=False)}\n\n"
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
        work_trajectory=state.work_trajectory.model_copy(deep=True),
        run_budget=state.run_budget,
        expert_context=state.expert_context,
        skill_context=state.skill_context,
        skill_observations=list(state.skill_observations),
    )
