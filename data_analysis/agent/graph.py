"""串联 Agent 决策、工具执行和状态更新。"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator, Iterable, Iterator
from contextlib import nullcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .loop import Action, ActionBatch, AgentLoop, Finish, IterationLimitError
from .planner import Planner, merge_revised_plan
from .verifier import Verification, Verifier
from .state import AgentState, AnalysisPlan, Observation, TodoItem, clip_text
from ..cancellation import TaskCancelledError
from ..observability.trace import RunTrace
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

GraphEvent = Action | SkillAction | Observation | SkillObservation | Finish | Verification

# Todo 结果进入后续 prompt 的字符预算，避免长回答被反复整段复制
MAX_RESULT_SUMMARY_CHARS = 2_000          # 单个 Todo 结果摘要的字符上限
MAX_PLAN_RESULTS_CHARS = 16_000           # 单次 prompt 内全部已完成结果的总预算
LOGGER = logging.getLogger(__name__)


class PlanCreated(BaseModel):
    """Planner 已生成顶层 Todo List。"""

    model_config = ConfigDict(frozen=True)
    plan: AnalysisPlan
    revision_kind: Literal["initial", "failure", "adaptive", "early_finish"] = "initial"


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
    *,
    verifier: Verifier | None = None,
    max_verify_retries: int = 1,
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
        decision = _step_decision(loop, state)
        if isinstance(decision, Finish):
            yield from _verify_delivery(decision, state, loop, harness, skill_runtime,
                                        verifier, max_verify_retries)
            return
        failure_summary = yield from _run_decision(
            decision, state, harness, repeats, failures, skill_runtime, skill_repeats,
        )
        if failure_summary is not None:
            raise RepeatedToolFailureError(failure_summary)



def run_graph(
    state: AgentState,
    loop: AgentLoop,
    harness: Harness,
    skill_runtime: SkillRuntime | None = None,
    *,
    verifier: Verifier | None = None,
    max_verify_retries: int = 1,
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
    for event in iter_graph(state, loop, harness, skill_runtime,
                            verifier=verifier, max_verify_retries=max_verify_retries):
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
    trace: RunTrace | None = None,
    adaptive_replan_enabled: bool = False,
    max_adaptive_replans: int = 2,
    replan_check_interval: int = 0,
    max_plan_reviews: int = 6,
    verifier: Verifier | None = None,
    max_verify_retries: int = 1,
) -> Iterator[PlanGraphEvent]:
    """按原事件语义调度计划，并可选采集规划与 Todo 片段。

    Args:
        state: 本次运行的可变状态。
        planner: 顶层规划器。
        loop: ReAct 决策循环。
        harness: 工具执行框架。
        skill_runtime: 可选技能运行时。
        failure_threshold: 默认 3 次相同失败熔断；调大允许更多失败重试。
        max_replans: 默认最多恢复规划 2 次；调大增加恢复机会和模型开销。
        max_todo_steps: 默认每个 Todo 30 步；调大允许更复杂子任务。
        trace: 当前运行独享的追踪容器；省略时不采集。
        adaptive_replan_enabled: 库入口默认关闭以兼容旧调用，服务端默认开启。
        max_adaptive_replans: 默认最多 2 次成功路径的计划改变，与失败恢复独立；0 禁用。
        replan_check_interval: 默认 0 只在 Todo 边界复审；正数按决策批次间隔复审，越小开销越高。
        max_plan_reviews: 默认最多 6 次复审（含继续和失败）；0 禁用，限制额外模型开销。
        verifier: 独立交付验证器；省略时保留旧库调用行为。
        max_verify_retries: 默认只允许 1 次失败修订，0 只验证不修订；不得超过 3。
    """
    if type(max_verify_retries) is not int or not 0 <= max_verify_retries <= 3:
        raise ValueError("验证修订次数必须为 0 到 3 的整数")
    source = _iter_plan_graph(
        state, planner, loop, harness, skill_runtime=skill_runtime,
        failure_threshold=failure_threshold, max_replans=max_replans,
        max_todo_steps=max_todo_steps, trace=trace,
        adaptive_replan_enabled=adaptive_replan_enabled, max_adaptive_replans=max_adaptive_replans,
        replan_check_interval=replan_check_interval, max_plan_reviews=max_plan_reviews,
    )

    active = None

    def deliver() -> Iterator[PlanGraphEvent]:
        """覆盖计划直接回答和正常汇总两个出口，候选答案不提前写入历史。"""
        nonlocal active
        try:
            for event in source:
                if isinstance(event, Finish):
                    if trace is not None:
                        trace.end(active)
                        active = None
                    yield from _verify_delivery(event, state, loop, harness, skill_runtime,
                                                verifier, max_verify_retries, trace)
                    return
                yield event
        finally:
            source.close()

    iterator = deliver()
    if trace is None:
        yield from iterator
        return
    active = trace.begin("plan", "create_plan")
    try:
        for event in iterator:
            if isinstance(event, (PlanCreated, Finish, Verification)):
                interrupted = isinstance(event, PlanCreated) and event.revision_kind in {"adaptive", "early_finish"}
                if interrupted and active is not None and active.kind == "todo":
                    trace.end(active, status="cancelled", error_type="PlanRevised")
                else:
                    trace.end(active)
                active = None
            elif isinstance(event, TodoStarted):
                active = trace.begin("todo", event.todo_id)
            elif isinstance(event, TodoCompleted):
                trace.end(active)
                active = None
            elif isinstance(event, TodoFailed):
                trace.end(active, status="error", error_type="TodoFailed")
                active = None
            yield event
            if isinstance(event, TodoFailed):
                active = trace.begin("plan", "recovery_plan")
    except BaseException as exc:
        trace.end(
            active,
            status="cancelled" if isinstance(exc, (TaskCancelledError, GeneratorExit)) else "error",
            error_type=type(exc).__name__,
        )
        raise
    finally:
        iterator.close()


def _iter_plan_graph(
    state: AgentState,
    planner: Planner,
    loop: AgentLoop,
    harness: Harness,
    *,
    skill_runtime: SkillRuntime | None = None,
    failure_threshold: int = 3,
    max_replans: int = 2,
    max_todo_steps: int = 30,
    trace: RunTrace | None = None,
    adaptive_replan_enabled: bool = False,
    max_adaptive_replans: int = 2,
    replan_check_interval: int = 0,
    max_plan_reviews: int = 6,
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
    if any(type(value) is not int or value < 0 for value in (max_adaptive_replans, replan_check_interval, max_plan_reviews)):
        raise ValueError("复审间隔与预算必须为非负整数")
    plan = planner.create_plan(state)
    if isinstance(plan, Finish):
        yield plan
        return
    state.analysis_plan = plan
    replan_count = 0
    yield PlanCreated(plan=plan)
    last_review_iteration = -1
    finished_early = False

    def maybe_reassess(trigger: str) -> PlanCreated | None:
        """只提供复审时机；有新观察、预算未耗尽时才请求模型判断。"""
        nonlocal plan, last_review_iteration, finished_early
        if not adaptive_replan_enabled or not callable(getattr(planner, "reassess", None)):
            return None
        if state.plan_reviews >= max_plan_reviews or state.adaptive_replans >= max_adaptive_replans:
            return None
        if not state.recent_observations or state.run_budget.iterations == last_review_iteration:
            return None
        if not any(item.status in {"pending", "in_progress"} for item in plan.todos):
            return None
        last_review_iteration = state.run_budget.iterations
        state.plan_reviews += 1
        scope = trace.span("plan", f"reassess_{trigger}") if trace else nullcontext()
        try:
            with scope:
                decision = planner.reassess(state, plan, trigger)
                if decision.action == "continue":
                    return None
                if decision.action == "revise":
                    revised = merge_revised_plan(plan, decision.todos, state.adaptive_replans + 1)
                    revision_kind = "adaptive"
                else:
                    revised = plan.model_copy(deep=True)
                    for item in revised.todos:
                        if item.status in {"pending", "in_progress"}:
                            item.status = "skipped"
                            item.result_summary = f"未执行；计划复审后省略：{decision.reasoning_summary}"
                    revision_kind = "early_finish"
                revised.reasoning_summary = decision.reasoning_summary
        except TaskCancelledError:
            raise
        except Exception as exc:
            # 可选复审失败不破坏已经可执行的计划；预算仍计入，避免反复请求。
            LOGGER.warning("计划复审未完成，继续原计划：%s", type(exc).__name__)
            state.work_trajectory.add_note("本次可选计划复审失败，保留原计划继续执行。")
            return None
        state.adaptive_replans += 1
        state.current_todo_id = None
        state.work_trajectory.add_decision(f"计划复审 {decision.action}：{decision.reasoning_summary}")
        plan = revised
        state.analysis_plan = plan
        finished_early = revision_kind == "early_finish"
        return PlanCreated(plan=plan, revision_kind=revision_kind)

    while not finished_early:
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
            decision = _step_decision(loop, todo_state)
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
            failure_summary = yield from _run_decision(
                decision, todo_state, harness, repeats, failures,
                skill_runtime, skill_repeats, parent_state=state, trace=trace,
            )
            if failure_summary is not None:
                break
            if replan_check_interval > 0 and todo_state.iteration % replan_check_interval == 0:
                revision_event = maybe_reassess("decision_interval")
                if revision_event is not None:
                    yield revision_event
                    break

        if failure_summary is None:
            revision_event = maybe_reassess("todo_completed")
            if revision_event is not None:
                yield revision_event
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
        state.failure_replans += 1
        plan = _merge_recovery_plan(plan, recovery, replan_count)
        state.analysis_plan = plan
        yield PlanCreated(plan=plan, revision_kind="failure")

    final_state = _final_state(state, plan)
    final_failures = FailureTracker(threshold=failure_threshold)
    final_repeats = RepeatGuard()
    final_skill_repeats: set[str] = set()
    while True:
        decision = _step_decision(loop, final_state)
        if isinstance(decision, Finish):
            yield decision
            return
        failure_summary = yield from _run_decision(
            decision, final_state, harness, final_repeats, final_failures,
            skill_runtime, final_skill_repeats, parent_state=state, trace=trace,
        )
        if failure_summary is not None:
            raise RepeatedToolFailureError(failure_summary)



def _verify_delivery(
    candidate: Finish, state: AgentState, loop: AgentLoop, harness: Harness,
    skill_runtime: SkillRuntime | None, verifier: Verifier | None,
    max_retries: int, trace: RunTrace | None = None,
) -> Iterator[GraphEvent]:
    """在同一运行预算内补查并复核，最终只提交一次用户可见回答。"""
    if type(max_retries) is not int or not 0 <= max_retries <= 3:
        raise ValueError("验证修订次数必须为 0 到 3 的整数")
    if verifier is None:
        state.append_message("user", state.user_query)
        state.append_message("assistant", candidate.answer)
        yield candidate
        return
    repair = _final_state(state, state.analysis_plan) if state.analysis_plan else state.model_copy(deep=True)
    repair.run_budget = state.run_budget
    failures, repeats, skill_repeats = FailureTracker(), RepeatGuard(), set()
    attempts, repairs = 0, 0
    while verifier is not None:
        result = verifier.verify(state, state.analysis_plan, candidate.answer)
        attempts += 1
        if result.status == "fail" and attempts <= max_retries:
            state.verification_summary = {**result.model_dump(), "attempts": attempts, "repairs": repairs}
            yield result
            repairs += 1
            feedback = Observation(tool_name="independent_verifier", success=True, result={
                "verification": result.model_dump(), "candidate_answer": clip_text(candidate.answer, 24_000),
                "instruction": "候选交付未通过，请按问题补查真实证据或修正回答，再返回 finish。不得把复核意见当成新的计算事实。",
            })
            repair.record_observation(feedback, count_iteration=False)
            repair_stopped = False
            while True:
                try:
                    decision = _step_decision(loop, repair)
                except IterationLimitError:
                    repair_stopped = True
                    break
                if isinstance(decision, Finish):
                    candidate = decision
                    break
                failure = yield from _run_decision(
                    decision, repair, harness, repeats, failures, skill_runtime,
                    skill_repeats, parent_state=state, trace=trace,
                )
                if failure is not None:
                    repair_stopped = True
                    break
            if not repair_stopped:
                continue
        if result.status == "fail":
            result = result.model_copy(update={"status": "uncertain"})
        state.verification_summary = {**result.model_dump(), "attempts": attempts, "repairs": repairs}
        yield result
        if result.status == "uncertain":
            disclosure = "\n\n> 独立验证：未完全通过。" + "；".join(issue.message for issue in result.issues)
            candidate = candidate.model_copy(update={"answer": candidate.answer + disclosure})
        break
    state.append_message("user", state.user_query)
    state.append_message("assistant", candidate.answer)
    yield candidate


def _step_decision(loop: AgentLoop, state: AgentState) -> ActionBatch | Action | SkillAction | Finish:
    """生产循环走批量入口，同时兼容旧调用方提供的单步替身。"""
    batch_step = getattr(loop, "step_batch", None)
    return batch_step(state) if callable(batch_step) else loop.step(state)


def _run_decision(
    decision: ActionBatch | Action | SkillAction,
    state: AgentState,
    harness: Harness,
    repeats: RepeatGuard,
    failures: FailureTracker,
    skill_runtime: SkillRuntime | None,
    skill_repeats: set[str],
    *,
    parent_state: AgentState | None = None,
    trace: RunTrace | None = None,
) -> Generator[GraphEvent, None, str | None]:
    """保持旧动作/观察事件协议；一批只消耗一个决策预算。"""
    if isinstance(decision, SkillAction):
        yield decision
        observation = _execute_skill_once(decision, skill_runtime, skill_repeats, trace)
        state.record_skill_observation(observation)
        if parent_state is not None:
            parent_state.absorb_skill_observation(observation)
        yield observation
        return None
    actions = decision.actions if isinstance(decision, ActionBatch) else [decision]
    parallel = len(actions) > 1 and harness.can_parallel(actions)
    if parallel:
        yield from actions
        observations = _execute_batch_once(actions, harness, repeats)
    failure_summary = None
    for index, action in enumerate(actions):
        if parallel:
            observation = observations[index]
        else:
            # 每个串行动作单独让出控制，HTTP 层仍可逐条等待人工批准。
            yield action
            observation = _execute_once(action, harness, repeats, trace)
        state.record_observation(observation, count_iteration=index == 0)
        if parent_state is not None:
            parent_state.absorb_observation(observation)
        yield observation
        circuit = failures.record(action, observation)
        if circuit is not None and failure_summary is None:
            failure_summary = _failure_summary(circuit)
            if not parallel:
                break
    return failure_summary


def _execute_batch_once(
    actions: list[Action], harness: Harness, repeats: RepeatGuard,
) -> list[Observation]:
    """先拦截历史重复和批内重复，再有序合并真正执行的结果。"""
    observations: dict[int, Observation] = {}
    pending: list[tuple[int, Action]] = []
    seen: set[str] = set()
    for index, action in enumerate(actions):
        fingerprint = json.dumps(
            {"tool": action.tool_name, "arguments": action.arguments},
            ensure_ascii=False, sort_keys=True,
        )
        notice = repeats.check(action)
        if notice is not None or fingerprint in seen:
            error = notice.message() if notice else "同一批次的相同工具与参数已拦截，请复用本批结果。"
            observations[index] = harness.rejected(action, error)
        else:
            seen.add(fingerprint)
            pending.append((index, action))
    results = harness.execute_many([action for _, action in pending])
    for (index, action), observation in zip(pending, results, strict=True):
        observations[index] = observation
        repeats.record(action, observation)
    return [observations[index] for index in range(len(actions))]


def _execute_skill_once(
    decision: SkillAction,
    runtime: SkillRuntime | None,
    fingerprints: set[str],
    trace: RunTrace | None = None,
) -> SkillObservation:
    """记录技能操作的耗时与状态，包含拒绝和重复拦截。"""
    if trace is None:
        return _execute_skill_untraced(decision, runtime, fingerprints)
    with trace.span("skill", f"{decision.skill_id}/{decision.operation}") as span:
        observation = _execute_skill_untraced(decision, runtime, fingerprints)
        trace.end(
            span, status="ok" if observation.success else "error",
            error_type=None if observation.success else "SkillExecutionError",
        )
        return observation


def _execute_skill_untraced(
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
    trace: RunTrace | None = None,
) -> Observation:
    """执行一次决策；参数完全相同的重复调用直接拦截，不再真正执行。

    重复调用不会带来新信息，却要付出工具耗时与上下文成本，因此在执行前
    拦截并返回一条带说明的观察，让模型改用已有结果或更换参数。
    """
    notice = repeats.check(decision)
    if notice is not None:
        reject = getattr(harness, "rejected", None)
        if trace is None and callable(reject):
            return reject(decision, notice.message())
        observation = Observation(
            tool_name=decision.tool_name,
            call_id=decision.call_id,
            success=False,
            error=notice.message(),
        )
        if trace is not None:
            span = trace.begin("tool", decision.tool_name)
            trace.end(span, status="error", error_type="RepeatedToolCall")
            observation = observation.model_copy(update={"span_id": span.span_id if span else None})
        return observation
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
    *,
    adaptive_replan_enabled: bool = False,
    max_adaptive_replans: int = 2,
    replan_check_interval: int = 0,
    max_plan_reviews: int = 6,
    verifier: Verifier | None = None,
    max_verify_retries: int = 1,
) -> Finish:
    """运行 Plan-and-Execute，直到最终汇总回答产生。"""
    for event in iter_plan_graph(
        state, planner, loop, harness, skill_runtime=skill_runtime,
        adaptive_replan_enabled=adaptive_replan_enabled, max_adaptive_replans=max_adaptive_replans,
        replan_check_interval=replan_check_interval, max_plan_reviews=max_plan_reviews,
        verifier=verifier, max_verify_retries=max_verify_retries,
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
                "status": todo.status,
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
        recent_observations=list(state.recent_observations),
    )


def _final_state(state: AgentState, plan: AnalysisPlan) -> AgentState:
    """构造最终汇总使用的独立 ReAct 状态。"""
    results = _project_results(plan.todos)
    query = (
        f"原始用户目标：{state.user_query}\n\n"
        f"执行计划结果：{json.dumps(results, ensure_ascii=False)}\n\n"
        "请综合所有 Todo 结果回答原始用户问题。不得声称未在结果中出现的事实；"
        "如确需补充核验，可以继续使用现有工具，否则直接返回 finish。"
        "skipped 表示尚未执行且被复审省略，不得把这些任务说成已完成；应交代证据边界。"
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
        recent_observations=list(state.recent_observations),
    )
