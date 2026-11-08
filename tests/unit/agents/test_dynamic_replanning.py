"""动态规划、观察可见与计划合并的关键回归。"""

import json
from types import SimpleNamespace

import pytest

from data_analysis.agent.graph import PlanCreated, iter_plan_graph
from data_analysis.agent.loop import Action, AgentLoop, Finish
from data_analysis.agent.planner import PlanDecision, Planner, merge_revised_plan
from data_analysis.agent.state import AgentState, AnalysisPlan, Observation
from data_analysis.runtime.harness import Harness
from data_analysis.tools.registry import ToolRegistry, ToolSpec


def _todo(todo_id, **kwargs):
    return {"id": todo_id, "objective": todo_id, "expected_outputs": ["证据"], **kwargs}


def _plan():
    return AnalysisPlan(objective="核验数据", todos=[_todo("read"), _todo("old", depends_on=["read"])])


def _registry():
    registry = ToolRegistry()
    registry.register_in_process(ToolSpec("read", "读取"), lambda args, ctx: {"rows": 7, "columns": ["actual"]})
    return registry


def test_planner_and_following_todo_see_bounded_actual_observations():
    state = AgentState(user_query="核验", analysis_plan=_plan())
    state.absorb_observation(Observation(tool_name="read", success=True, result={"values": list(range(30_000))}))
    planner = Planner(SimpleNamespace(), [])
    prompt = planner.build_messages(state)[-1][1]
    marker = '{\n  "user_query"'
    context = json.loads(prompt[prompt.rindex(marker):])
    assert "recent_observations" in context
    projected = json.dumps(context["recent_observations"], ensure_ascii=False)
    assert len(projected) < 20_000 and "截断" in projected
    from data_analysis.agent.graph import _todo_state

    todo_state = _todo_state(state, state.analysis_plan.todos[1])
    messages = AgentLoop(SimpleNamespace(), [], "按证据分析").build_messages(todo_state)
    assert "values" in messages[-1][1]
    assert state.observations == []
    response = '<think>不公开的内部内容</think>\n```json\n{"action":"continue","reasoning_summary":"继续核验"}\n```'
    review = Planner(SimpleNamespace(invoke=lambda messages: response), [], retries=0).reassess(
        state, state.analysis_plan, "todo_completed",
    )
    assert review.action == "continue" and review.reasoning_summary == "继续核验"


def test_revision_preserves_completed_result_and_resolves_dependencies():
    previous = _plan()
    previous.todos[0].status = "completed"
    previous.todos[0].result_summary = "已确认 actual 字段"
    revised = merge_revised_plan(previous, [
        _todo("check", depends_on=["read"]), _todo("report", depends_on=["check"]),
    ], 1)
    assert revised.todos[0] == previous.todos[0]
    assert revised.todos[1].depends_on == ["read"]
    assert revised.todos[2].depends_on == [revised.todos[1].id]
    with pytest.raises(ValueError):
        merge_revised_plan(previous, [_todo("a", depends_on=["missing"])], 2)
    with pytest.raises(ValueError):
        merge_revised_plan(previous, [_todo("a", depends_on=["b"]), _todo("b", depends_on=["a"])], 2)


def test_success_path_revises_remaining_plan_without_resetting_budget():
    registry = _registry()
    seen = []

    def reassess(state, plan, trigger):
        seen.append((trigger, state.recent_observations[-1].result))
        return PlanDecision(action="revise", reasoning_summary="已确认字段不同，改写后续核验", todos=[_todo("actual_check", depends_on=["read"])])

    decisions = iter([Action(tool_name="read"), Finish(answer="读取完成"), Finish(answer="核验完成"), Finish(answer="交付")])
    state = AgentState(user_query="核验数据")
    events = list(iter_plan_graph(
        state, SimpleNamespace(create_plan=lambda state: _plan(), reassess=reassess),
        SimpleNamespace(step=lambda state: next(decisions)), Harness(registry), adaptive_replan_enabled=True,
    ))
    assert len([event for event in events if isinstance(event, PlanCreated)]) == 2
    assert seen[0][1]["rows"] == 7
    assert state.analysis_plan.todos[0].status == "completed"
    assert state.adaptive_replans == 1 and state.failure_replans == 0
    assert state.run_budget.iterations == 1
    assert state.analysis_plan.todos[1].objective == "actual_check"


def test_interval_early_finish_marks_unexecuted_work_skipped_and_still_summarizes():
    registry = _registry()
    decisions = iter([Action(tool_name="read"), Finish(answer="依据真实 rows=7 汇总")])
    state = AgentState(user_query="核验数据")
    planner = SimpleNamespace(
        create_plan=lambda state: _plan(),
        reassess=lambda *args: PlanDecision(action="finish_early", reasoning_summary="已有证据足够"),
    )
    events = list(iter_plan_graph(
        state, planner, SimpleNamespace(step=lambda state: next(decisions)), Harness(registry),
        adaptive_replan_enabled=True, replan_check_interval=1,
    ))
    assert isinstance(events[-1], Finish)
    assert all(todo.status == "skipped" for todo in state.analysis_plan.todos)
    assert state.plan_reviews == 1 and state.run_budget.iterations == 1


def test_malformed_review_is_bounded_and_keeps_existing_plan():
    class Client:
        calls = 0

        def invoke(self, messages):
            self.calls += 1
            return '{"action":"revise","reasoning_summary":"无效","todos":[]}'

    client = Client()
    planner = Planner(client, [], retries=0)
    planner.create_plan = lambda state: _plan()
    decisions = iter([Action(tool_name="read"), Finish(answer="已读"), Finish(answer="完成"), Finish(answer="最终回答")])
    state = AgentState(user_query="核验")
    events = list(iter_plan_graph(
        state, planner, SimpleNamespace(step=lambda state: next(decisions)), Harness(_registry()),
        adaptive_replan_enabled=True, max_plan_reviews=1,
    ))
    assert isinstance(events[-1], Finish) and client.calls == 1
    assert state.plan_reviews == 1 and state.adaptive_replans == 0
    assert all(todo.status == "completed" for todo in state.analysis_plan.todos)
