"""Planner 与顶层 Plan-and-Execute 的契约测试。"""

from __future__ import annotations

import json

import pytest

from data_analysis.agent.graph import run_plan_graph
from data_analysis.agent.loop import AgentLoop
from data_analysis.agent.planner import Planner, PlannerError
from data_analysis.agent.state import AgentState, AnalysisPlan
from data_analysis.runtime import Harness
from data_analysis.tools import ToolRegistry


class ScriptedLlm:
    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.calls: list[list[tuple[str, str]]] = []

    def invoke(self, messages: list[tuple[str, str]]) -> str:
        self.calls.append(messages)
        return next(self._responses)


def _plan_response() -> str:
    return json.dumps({
        "objective": "完成数据分析",
        "todos": [
            {
                "id": "todo_1",
                "objective": "检查数据",
                "status": "pending",
                "depends_on": [],
                "expected_outputs": ["数据概况"],
                "result_summary": None,
            },
            {
                "id": "todo_2",
                "objective": "形成结论",
                "status": "pending",
                "depends_on": ["todo_1"],
                "expected_outputs": ["分析结论"],
                "result_summary": None,
            },
        ],
    }, ensure_ascii=False)


def test_planner_creates_valid_todo_list() -> None:
    llm = ScriptedLlm([_plan_response()])
    plan = Planner(llm, []).create_plan(AgentState(user_query="分析数据"))

    assert [todo.id for todo in plan.todos] == ["todo_1", "todo_2"]
    assert plan.todos[1].depends_on == ["todo_1"]
    assert "只生成简洁" in llm.calls[0][0][1]


def test_plan_rejects_dependency_cycle() -> None:
    with pytest.raises(ValueError, match="循环"):
        AnalysisPlan.model_validate({
            "objective": "错误计划",
            "todos": [
                {"id": "a", "objective": "A", "depends_on": ["b"]},
                {"id": "b", "objective": "B", "depends_on": ["a"]},
            ],
        })


def test_planner_rejects_more_than_configured_todos() -> None:
    response = json.dumps({
        "objective": "过长计划",
        "todos": [
            {"id": f"todo_{index}", "objective": str(index)}
            for index in range(3)
        ],
    })
    with pytest.raises(PlannerError, match="超过上限"):
        Planner(ScriptedLlm([response]), [], max_todos=2, retries=0).create_plan(
            AgentState(user_query="分析")
        )


def test_planner_rejects_precompleted_todo() -> None:
    response = json.dumps({
        "objective": "无效计划",
        "todos": [{
            "id": "todo_1",
            "objective": "跳过执行",
            "status": "completed",
            "result_summary": "伪造结果",
        }],
    }, ensure_ascii=False)

    with pytest.raises(PlannerError, match="pending"):
        Planner(ScriptedLlm([response]), [], retries=0).create_plan(
            AgentState(user_query="分析")
        )


def test_plan_graph_runs_todos_with_unchanged_react_then_summarizes() -> None:
    llm = ScriptedLlm([
        _plan_response(),
        '{"type":"finish","answer":"数据共有 10 行。"}',
        '{"type":"finish","answer":"数据质量满足分析要求。"}',
        '{"type":"finish","answer":"最终结论：数据质量满足要求。"}',
    ])
    state = AgentState(user_query="分析数据")
    registry = ToolRegistry()

    finish = run_plan_graph(
        state,
        Planner(llm, registry.specs()),
        AgentLoop(llm, registry.specs(), "依据当前 Todo 工作"),
        Harness(registry),
    )

    assert finish.answer == "最终结论：数据质量满足要求。"
    assert state.analysis_plan is not None
    assert [todo.status for todo in state.analysis_plan.todos] == [
        "completed",
        "completed",
    ]
    assert [todo.result_summary for todo in state.analysis_plan.todos] == [
        "数据共有 10 行。",
        "数据质量满足分析要求。",
    ]
    assert [message.role for message in state.messages] == ["user", "assistant"]
