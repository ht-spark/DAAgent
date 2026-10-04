"""Planner 与顶层 Plan-and-Execute 的契约测试。"""

from __future__ import annotations

import json

import pytest

from data_analysis.agent.graph import (
    TodoFailed,
    _merge_recovery_plan,
    _todo_state,
    iter_plan_graph,
    run_plan_graph,
)
from data_analysis.agent.loop import AgentLoop, Finish, IterationLimitError
from data_analysis.agent.planner import (
    MAX_PLANNER_HISTORY_CHARS,
    MAX_PLANNER_HISTORY_ITEM_CHARS,
    MAX_PLANNER_HISTORY_MESSAGES,
    Planner,
    PlannerError,
)
from data_analysis.agent.state import AgentMessage, AgentState, AnalysisPlan, Observation
from data_analysis.runtime import Harness, RepeatedToolFailureError
from data_analysis.tools import ToolRegistry, ToolSpec, create_agent_tool_registry


class ScriptedLlm:
    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.calls: list[list[tuple[str, str]]] = []

    def invoke(self, messages: list[tuple[str, str]]) -> str:
        self.calls.append(messages)
        return next(self._responses)


def _planner_context(prompt: str) -> dict:
    """从 Planner 提示词尾部取出注入的上下文 JSON。

    planner.md 模板里含示例 JSON，因此只能按上下文对象的首键定位。
    """
    marker = '{\n  "user_query"'
    return json.loads(prompt[prompt.rindex(marker):])


def _plan_response() -> str:
    return json.dumps({
        "objective": "完成数据分析",
        "reasoning_summary": "先核对数据质量，再基于可靠数据形成结论。",
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
    assert plan.reasoning_summary == "先核对数据质量，再基于可靠数据形成结论。"
    assert "指令优先级依次为" in llm.calls[0][0][1]
    assert "同一工具、同一参数不要重复调用" in llm.calls[0][0][1]
    assert "Planner 阶段遵循其独立的直接回答或计划 JSON 契约" in llm.calls[0][0][1]
    assert "简单请求直接回答" in llm.calls[0][1][1]
    assert "价值系统与优先级" in llm.calls[0][1][1]
    assert "事实真实性和证据边界优先于顺利完成" in llm.calls[0][1][1]
    assert "模型的正确性、适用性和可验证性优先" in llm.calls[0][1][1]
    assert "通用且可复用的解决方案优先" in llm.calls[0][1][1]
    assert "可追溯、可复现、可验收" in llm.calls[0][1][1]
    assert "Todo 之间职责不得重叠" in llm.calls[0][1][1]
    assert "优先检查并复用 `available_artifacts`" in llm.calls[0][1][1]
    assert "最多生成 8 个必要 Todo" in llm.calls[0][1][1]
    assert "最短充分证据链" in llm.calls[0][1][1]
    assert "Todo 的 `objective` 必须是短摘要" in llm.calls[0][1][1]
    assert "不超过 30 个中文字符" in llm.calls[0][1][1]
    assert "不得把数据质量问题直接解释为业务变化" in llm.calls[0][1][1]
    assert "不能直接表述为因果关系" in llm.calls[0][1][1]
    assert "问候或寒暄" in llm.calls[0][1][1]
    assert "如果计划只会包含“回答用户”这一个 Todo" in llm.calls[0][1][1]
    assert "{{CONTEXT_JSON}}" not in llm.calls[0][1][1]


def test_simple_greeting_returns_direct_answer_without_plan() -> None:
    """问候类请求应由 Planner 直接回答，不产生 Todo 事件。"""
    response = json.dumps({
        "type": "finish",
        "answer": "你好！有什么可以帮你？",
    }, ensure_ascii=False)
    llm = ScriptedLlm([response])
    state = AgentState(user_query="你好")

    events = list(iter_plan_graph(
        state,
        Planner(llm, []),
        AgentLoop(llm, [], "依据当前 Todo 工作"),
        Harness(ToolRegistry()),
    ))

    assert len(events) == 1
    assert isinstance(events[0], Finish)
    assert events[0].answer == "你好！有什么可以帮你？"
    assert events[0].thought == ""
    assert state.analysis_plan is None
    assert [message.role for message in state.messages] == ["user", "assistant"]


def test_planner_context_includes_memory_and_work_trajectory() -> None:
    """Planner 必须拿到滚动摘要与结构化轨迹，避免跨轮重复规划。"""
    from data_analysis.agent.state import WorkTrajectory

    trajectory = WorkTrajectory()
    trajectory.add_read("data/sales.csv")
    trajectory.add_modified("outputs/report.md")
    trajectory.add_decision("已确认口径为含税销售额")
    trajectory.add_note("profile_data: rows=150, missing_rate=0.02")

    state = AgentState(
        user_query="继续分析复购",
        memory_summary="## Goal\n分析销量\n## Files & Artifacts\nsales.csv",
        work_trajectory=trajectory,
    )
    llm = ScriptedLlm([_plan_response()])
    Planner(llm, []).create_plan(state)

    context = llm.calls[0][1][1]
    assert '"session_memory"' in context
    assert "分析销量" in context
    assert '"files_read"' in context
    assert "data/sales.csv" in context
    assert "outputs/report.md" in context
    assert "已确认口径为含税销售额" in context
    assert "profile_data: rows=150" in context


def test_plan_rejects_dependency_cycle() -> None:
    with pytest.raises(ValueError, match="循环"):
        AnalysisPlan.model_validate({
            "objective": "错误计划",
            "todos": [
                {
                    "id": "a",
                    "objective": "A",
                    "depends_on": ["b"],
                    "expected_outputs": ["A 的验证结果"],
                },
                {
                    "id": "b",
                    "objective": "B",
                    "depends_on": ["a"],
                    "expected_outputs": ["B 的验证结果"],
                },
            ],
        })


def test_planner_rejects_more_than_configured_todos() -> None:
    response = json.dumps({
        "objective": "过长计划",
        "todos": [
            {
                "id": f"todo_{index}",
                "objective": str(index),
                "expected_outputs": [f"结果 {index}"],
            }
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
            "expected_outputs": ["实际执行结果"],
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
        "todos": [{
            "id": "todo_1",
            "objective": "执行原方案",
            "expected_outputs": ["原方案验证结果"],
        }],
    }, ensure_ascii=False)
    recovery_plan = json.dumps({
        "objective": "恢复任务",
        "todos": [{
            "id": "todo_1",
            "objective": "使用替代方案",
            "expected_outputs": ["替代方案验证结果"],
        }],
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
            {
                "id": "todo_1",
                "objective": "生成报告",
                "expected_outputs": ["report.md 报告文件"],
            },
            {
                "id": "todo_2",
                "objective": "复用已有报告给出结论",
                "depends_on": ["todo_1"],
                "expected_outputs": ["经核验的报告结论"],
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


@pytest.mark.parametrize("expected_outputs", [[], ["   "]])
def test_todo_rejects_missing_or_blank_expected_outputs(
    expected_outputs: list[str],
) -> None:
    with pytest.raises(ValueError, match="预期输出|at least 1 item"):
        AnalysisPlan.model_validate({
            "objective": "无效计划",
            "todos": [{
                "id": "todo_1",
                "objective": "执行任务",
                "expected_outputs": expected_outputs,
            }],
        })


def test_planner_context_includes_recent_conversation() -> None:
    """短会话下 Planner 也必须看到最近原文，才能理解追问所指。"""
    state = AgentState(
        user_query="按上面那个口径再算一遍",
        messages=[
            AgentMessage(role="user", content="先看总量"),
            AgentMessage(role="assistant", content="采用含税口径，全量合计 100 万"),
        ],
    )
    llm = ScriptedLlm([_plan_response()])

    Planner(llm, []).create_plan(state)

    context = llm.calls[0][1][1]
    assert '"recent_conversation"' in context
    assert "采用含税口径，全量合计 100 万" in context


def test_planner_recent_conversation_is_bounded() -> None:
    """最近对话必须按条数与字符预算裁剪。"""
    state = AgentState(
        user_query="继续",
        messages=[
            AgentMessage(role="user", content=f"{index}：" + "长" * 4000)
            for index in range(30)
        ],
    )
    llm = ScriptedLlm([_plan_response()])

    Planner(llm, []).create_plan(state)

    recent = _planner_context(llm.calls[0][1][1])["recent_conversation"]

    assert 0 < len(recent) <= MAX_PLANNER_HISTORY_MESSAGES
    assert all(len(item["content"]) <= MAX_PLANNER_HISTORY_ITEM_CHARS for item in recent)
    assert sum(len(item["content"]) for item in recent) <= (
        MAX_PLANNER_HISTORY_CHARS + MAX_PLANNER_HISTORY_ITEM_CHARS
    )
    # 保留的是最近轮次
    assert recent[-1]["content"].startswith("29：")


def test_todo_state_shares_run_budget_and_clips_result_summaries() -> None:
    """Todo 的内部状态必须共用运行预算，且不整段复制超长结果。"""
    plan = AnalysisPlan.model_validate({
        "objective": "生成并复核报告",
        "todos": [
            {
                "id": "todo_1",
                "objective": "写报告",
                "expected_outputs": ["报告文件"],
                "status": "completed",
                "result_summary": "甲" * 5000,
            },
            {
                "id": "todo_2",
                "objective": "复核报告",
                "expected_outputs": ["复核结论"],
            },
        ],
    })
    state = AgentState(user_query="生成并复核报告", analysis_plan=plan)

    todo_state = _todo_state(state, plan.todos[1])

    assert todo_state.run_budget is state.run_budget
    assert "（已截断）" in todo_state.user_query
    assert "甲" * 2000 not in todo_state.user_query
    assert "复核报告" in todo_state.user_query


def test_merge_recovery_plan_maps_and_drops_dependencies() -> None:
    """恢复计划的依赖需重映射；遇到未经校验的越界引用也不能崩。"""
    previous = AnalysisPlan.model_validate({
        "objective": "目标",
        "todos": [{
            "id": "todo_1",
            "objective": "已完成",
            "expected_outputs": ["结果"],
            "status": "completed",
            "result_summary": "完成",
        }],
    })
    recovery = AnalysisPlan.model_validate({
        "objective": "恢复目标",
        "todos": [
            {"id": "a", "objective": "A", "expected_outputs": ["结果 A"]},
            {"id": "b", "objective": "B", "expected_outputs": ["结果 B"], "depends_on": ["a"]},
        ],
    })
    # 模拟未经校验的计划：依赖里出现未知标识与已保留的历史标识
    unvalidated = recovery.model_copy(deep=True)
    unvalidated.todos[1].depends_on = ["a", "todo_1", "ghost"]

    merged = _merge_recovery_plan(previous, unvalidated, 1)

    assert [todo.id for todo in merged.todos] == [
        "todo_1",
        "recovery_1_a",
        "recovery_1_b",
    ]
    assert merged.todos[2].depends_on == ["recovery_1_a", "todo_1"]


def _noop_registry(result: dict[str, object]) -> ToolRegistry:
    """构造只含一个无操作工具的注册表。"""
    registry = ToolRegistry()
    registry.register_in_process(
        ToolSpec(name="noop", description="无操作测试工具"),
        lambda _arguments, _context: result,
    )
    return registry


def test_plan_graph_shares_one_iteration_budget_across_todos() -> None:
    """整轮 run 的决策预算必须跨 Todo 共享，不能被逐个 Todo 重置。"""
    plan = json.dumps({
        "objective": "两步操作",
        "todos": [
            {"id": "todo_1", "objective": "第一步", "expected_outputs": ["结果一"]},
            {
                "id": "todo_2",
                "objective": "第二步",
                "expected_outputs": ["结果二"],
                "depends_on": ["todo_1"],
            },
        ],
    }, ensure_ascii=False)
    action = json.dumps(
        {"type": "action", "tool_name": "noop", "arguments": {}},
        ensure_ascii=False,
    )
    llm = ScriptedLlm([
        plan,
        action,
        action,
        '{"type":"finish","answer":"第一步完成。"}',
        action,
        '{"type":"finish","answer":"第二步完成。"}',
    ])
    registry = _noop_registry({"ok": True})
    state = AgentState(user_query="完成任务")

    with pytest.raises(IterationLimitError):
        run_plan_graph(
            state,
            Planner(llm, registry.specs()),
            AgentLoop(llm, registry.specs(), "依据当前 Todo 工作", max_iterations=3),
            Harness(registry),
        )

    assert state.run_budget.iterations == 3
    # 预算耗尽后不再调用模型
    assert len(llm.calls) == 5


def test_plan_graph_keeps_run_state_free_of_full_observations() -> None:
    """运行级状态只聚合轨迹，不重复留存完整工具结果。"""
    plan = json.dumps({
        "objective": "核对数据",
        "todos": [{
            "id": "todo_1",
            "objective": "核对数据规模",
            "expected_outputs": ["数据规模结论"],
        }],
    }, ensure_ascii=False)
    action = json.dumps(
        {"type": "action", "tool_name": "noop", "arguments": {}},
        ensure_ascii=False,
    )
    llm = ScriptedLlm([
        plan,
        action,
        '{"type":"finish","answer":"共 150 行。"}',
        '{"type":"finish","answer":"任务完成。"}',
    ])
    registry = _noop_registry({"summary": {"rows": 150}})
    state = AgentState(user_query="核对数据")

    finish = run_plan_graph(
        state,
        Planner(llm, registry.specs()),
        AgentLoop(llm, registry.specs(), "依据当前 Todo 工作"),
        Harness(registry),
    )

    assert finish.answer == "任务完成。"
    assert state.observations == []
    assert state.run_budget.iterations == 1
    assert any("rows=150" in note for note in state.work_trajectory.notes)


def test_plan_graph_blocks_duplicate_identical_calls() -> None:
    """参数完全相同的重复调用只执行一次，其余被拦截并提示复用已有结果。"""
    plan = json.dumps({
        "objective": "核对数据",
        "todos": [{
            "id": "todo_1",
            "objective": "核对数据规模",
            "expected_outputs": ["数据规模结论"],
        }],
    }, ensure_ascii=False)
    action = json.dumps(
        {"type": "action", "tool_name": "noop", "arguments": {"value": 1}},
        ensure_ascii=False,
    )
    llm = ScriptedLlm([
        plan,
        action,
        action,
        '{"type":"finish","answer":"共 1 条记录。"}',
        '{"type":"finish","answer":"任务完成。"}',
    ])
    executed: list[int] = []
    registry = ToolRegistry()
    registry.register_in_process(
        ToolSpec(name="noop", description="无操作测试工具"),
        lambda arguments, _context: (
            executed.append(arguments["value"]) or {"summary": "value=1"}
        ),
    )
    state = AgentState(user_query="核对数据")

    events = list(iter_plan_graph(
        state,
        Planner(llm, registry.specs()),
        AgentLoop(llm, registry.specs(), "依据当前 Todo 工作"),
        Harness(registry),
    ))

    assert executed == [1]
    blocked = [
        event
        for event in events
        if isinstance(event, Observation) and not event.success
    ]
    assert any("重复调用已拦截" in (event.error or "") for event in blocked)
    assert any(
        "重复调用已拦截" in failure for failure in state.work_trajectory.failures
    )


def test_plan_graph_fails_todo_at_step_limit_instead_of_looping() -> None:
    """单个 Todo 超过步数上限时按失败处理并交给恢复规划，而不是继续空转。"""
    plan = json.dumps({
        "objective": "核对数据",
        "todos": [{
            "id": "todo_1",
            "objective": "反复核对",
            "expected_outputs": ["核对结论"],
        }],
    }, ensure_ascii=False)
    actions = [
        json.dumps(
            {"type": "action", "tool_name": "noop", "arguments": {"value": index}},
            ensure_ascii=False,
        )
        for index in range(1, 4)
    ]
    llm = ScriptedLlm([plan, *actions])
    registry = _noop_registry({"summary": "ok"})
    state = AgentState(user_query="核对数据")

    with pytest.raises(RepeatedToolFailureError, match="决策轮数上限"):
        list(iter_plan_graph(
            state,
            Planner(llm, registry.specs()),
            AgentLoop(llm, registry.specs(), "依据当前 Todo 工作"),
            Harness(registry),
            max_todo_steps=2,
            max_replans=0,
        ))

    assert state.run_budget.iterations == 2
    assert state.analysis_plan is not None
    assert state.analysis_plan.todos[0].status == "failed"
    assert "决策轮数上限" in (state.analysis_plan.todos[0].result_summary or "")
