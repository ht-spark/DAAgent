"""Planner 与顶层 Plan-and-Execute 的契约测试。"""

from __future__ import annotations

import json

import pytest

from data_analysis.agent.graph import TodoFailed, iter_plan_graph, run_plan_graph
from data_analysis.agent.loop import AgentLoop
from data_analysis.agent.planner import Planner, PlannerError
from data_analysis.agent.state import AgentState, AnalysisPlan
from data_analysis.runtime import Harness
from data_analysis.tools import ToolRegistry, ToolSpec, create_agent_tool_registry


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


def test_plan_graph_replans_after_repeated_identical_failure() -> None:
    initial_plan = json.dumps({
        "objective": "完成任务",
        "todos": [{"id": "todo_1", "objective": "执行原方案"}],
    }, ensure_ascii=False)
    recovery_plan = json.dumps({
        "objective": "恢复任务",
        "todos": [{"id": "todo_1", "objective": "使用替代方案"}],
    }, ensure_ascii=False)
    repeated_action = json.dumps({
        "type": "action",
        "tool_name": "always_fail",
        "arguments": {"value": 1},
    }, ensure_ascii=False)
    llm = ScriptedLlm([
        initial_plan,
        repeated_action,
        repeated_action,
        repeated_action,
        recovery_plan,
        '{"type":"finish","answer":"替代方案完成。"}',
        '{"type":"finish","answer":"任务最终完成。"}',
    ])
    registry = ToolRegistry()

    def always_fail(_arguments, _context):
        raise ValueError("固定失败")

    registry.register_in_process(
        ToolSpec(name="always_fail", description="始终失败的测试工具"),
        always_fail,
    )
    state = AgentState(user_query="完成任务")

    events = list(iter_plan_graph(
        state,
        Planner(llm, registry.specs()),
        AgentLoop(llm, registry.specs(), "依据当前 Todo 工作"),
        Harness(registry),
    ))

    assert any(isinstance(event, TodoFailed) for event in events)
    assert state.analysis_plan is not None
    assert [todo.status for todo in state.analysis_plan.todos] == ["failed", "completed"]
    assert state.analysis_plan.todos[1].id == "recovery_1_todo_1"
    assert "不得原样重复" in llm.calls[4][1][1]


def test_plan_graph_passes_new_artifact_to_later_todo_context(tmp_path) -> None:
    plan = json.dumps({
        "objective": "生成并复用报告",
        "todos": [
            {"id": "todo_1", "objective": "生成报告"},
            {
                "id": "todo_2",
                "objective": "复用已有报告给出结论",
                "depends_on": ["todo_1"],
            },
        ],
    }, ensure_ascii=False)
    write_action = json.dumps({
        "type": "action",
        "tool_name": "write_artifact",
        "arguments": {
            "relative_path": "report.md",
            "content": "# 分析结果",
        },
    }, ensure_ascii=False)
    llm = ScriptedLlm([
        plan,
        write_action,
        '{"type":"finish","answer":"报告已生成。"}',
        '{"type":"finish","answer":"已复用报告。"}',
        '{"type":"finish","answer":"任务完成。"}',
    ])
    output_dir = tmp_path / "outputs"
    registry = create_agent_tool_registry([], output_dir)
    state = AgentState(
        user_query="生成报告并在后续步骤复用",
        output_dir=str(output_dir),
    )

    finish = run_plan_graph(
        state,
        Planner(llm, registry.specs()),
        AgentLoop(llm, registry.specs(), "依据当前 Todo 工作"),
        Harness(registry),
    )

    assert finish.answer == "任务完成。"
    assert state.available_artifacts[0].relative_path == "report.md"
    second_todo_context = llm.calls[3][-1][1]
    assert '"artifact_reuse_status": "已有产物可直接复用"' in second_todo_context
    assert '"relative_path": "report.md"' in second_todo_context
    assert str((output_dir / "report.md").resolve()).replace("\\", "\\\\") in (
        second_todo_context
    )
