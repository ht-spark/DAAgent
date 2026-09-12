from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from data_analysis.agent.graph import iter_graph, run_graph
from data_analysis.agent.loop import Action, AgentLoop, Finish, IterationLimitError
from data_analysis.agent.state import AgentState, Observation
from data_analysis.runtime import Harness
from data_analysis.tools import ToolRegistry, create_data_profile_registry


class ScriptedLlm:
    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.calls: list[list[tuple[str, str]]] = []

    def invoke(self, messages: list[tuple[str, str]]) -> str:
        self.calls.append(messages)
        return next(self._responses)


class FakeSandbox:
    def __init__(self, result: Any = None) -> None:
        self.result = result if result is not None else {"ok": True}
        self.call_count = 0

    def execute(self, tool, arguments) -> Any:
        self.call_count += 1
        return self.result


def test_graph_runs_profile_action_then_finishes(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1, 2, 3]}).to_csv(path, index=False)
    registry = create_data_profile_registry([path])
    llm = ScriptedLlm(
        [
            '{"type":"action","thought":"先读取数据",'
            '"tool_name":"profile_data","arguments":{}}',
            '{"type":"finish","thought":"画像信息充分",'
            '"answer":"数据包含 3 行销量记录。"}',
        ]
    )
    state = AgentState(user_query="生成基础数据画像", data_paths=[str(path)])

    finish = run_graph(
        state,
        AgentLoop(llm, registry.specs(), "必须依据工具观察回答"),
        Harness(registry),
    )

    assert finish.answer == "数据包含 3 行销量记录。"
    assert state.iteration == 1
    assert state.observations[0].success is True
    assert state.observations[0].result["summary"]["row_count"] == 3
    assert [message.role for message in state.messages] == ["user", "assistant"]
    assert state.messages[-1].content == finish.answer
    assert len(llm.calls) == 2
    assert '"row_count": 3' in llm.calls[1][-1][1]


def test_graph_returns_immediate_finish_without_tool_call() -> None:
    llm = ScriptedLlm(
        ['{"type":"finish","thought":"无需工具","answer":"线性回归用于拟合线性关系。"}']
    )
    sandbox = FakeSandbox()
    state = AgentState(user_query="什么是线性回归？")

    finish = run_graph(
        state,
        AgentLoop(llm, [], "根据当前信息回答"),
        Harness(ToolRegistry(), sandbox=sandbox),
    )

    assert finish == Finish(
        thought="无需工具",
        answer="线性回归用于拟合线性关系。",
    )
    assert state.iteration == 0
    assert state.observations == []
    assert sandbox.call_count == 0
    assert [message.content for message in state.messages] == [
        "什么是线性回归？",
        "线性回归用于拟合线性关系。",
    ]


def test_graph_feeds_failed_observation_back_to_loop(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1]}).to_csv(path, index=False)
    registry = create_data_profile_registry([path])
    llm = ScriptedLlm(
        [
            '{"type":"action","tool_name":"profile_data",'
            '"arguments":{"data_paths":["other.csv"]}}',
            '{"type":"finish","thought":"工具失败",'
            '"answer":"数据画像执行失败，请检查输入。"}',
        ]
    )
    state = AgentState(user_query="分析数据", data_paths=[str(path)])

    finish = run_graph(
        state,
        AgentLoop(llm, registry.specs(), "必须依据工具观察回答"),
        Harness(registry),
    )

    assert finish.answer == "数据画像执行失败，请检查输入。"
    assert state.observations[0].success is False
    assert "ToolArgumentsError" in state.observations[0].error
    assert '"success": false' in llm.calls[1][-1][1]
    assert "ToolArgumentsError" in llm.calls[1][-1][1]


def test_graph_stops_at_loop_iteration_limit(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1]}).to_csv(path, index=False)
    registry = create_data_profile_registry([path])
    action = '{"type":"action","tool_name":"profile_data","arguments":{}}'
    llm = ScriptedLlm([action, action, action])
    sandbox = FakeSandbox({"summary": {"row_count": 1}})
    state = AgentState(user_query="反复分析", data_paths=[str(path)])

    with pytest.raises(IterationLimitError, match="最大决策轮数 2"):
        run_graph(
            state,
            AgentLoop(
                llm,
                registry.specs(),
                "必须依据工具观察回答",
                max_iterations=2,
            ),
            Harness(registry, sandbox=sandbox),
        )

    assert state.iteration == 2
    assert len(state.observations) == 2
    assert sandbox.call_count == 2
    assert state.messages == []


def test_iter_graph_exposes_action_observation_and_finish(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1, 2]}).to_csv(path, index=False)
    registry = create_data_profile_registry([path])
    llm = ScriptedLlm(
        [
            '{"type":"action","thought":"读取数据概况",'
            '"tool_name":"profile_data","arguments":{}}',
            '{"type":"finish","thought":"画像完成",'
            '"answer":"数据共有 2 行。"}',
        ]
    )
    state = AgentState(user_query="分析数据", data_paths=[str(path)])

    events = list(iter_graph(
        state,
        AgentLoop(llm, registry.specs(), "依据数据回答"),
        Harness(registry),
    ))

    assert [type(event) for event in events] == [Action, Observation, Finish]
    assert events[0].thought == "读取数据概况"
    assert events[1].success is True
    assert events[2].answer == "数据共有 2 行。"


def test_graph_can_finish_without_using_available_data_tool(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1, 2]}).to_csv(path, index=False)
    registry = create_data_profile_registry([path])
    llm = ScriptedLlm(['{"type":"finish","answer":"均值是算术平均数。"}'])
    sandbox = FakeSandbox()
    state = AgentState(user_query="解释一下均值", data_paths=[str(path)])

    finish = run_graph(
        state,
        AgentLoop(llm, registry.specs(), "根据问题自主决定是否调用工具"),
        Harness(registry, sandbox=sandbox),
    )

    assert finish.answer == "均值是算术平均数。"
    assert state.observations == []
    assert sandbox.call_count == 0
