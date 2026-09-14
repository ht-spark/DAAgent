from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest

from data_analysis.agent.graph import iter_graph, run_graph
from data_analysis.agent.loop import Action, AgentLoop, Finish, IterationLimitError
from data_analysis.agent.state import AgentState, Observation
from data_analysis.runtime import Harness
from data_analysis.tools import (
    ToolRegistry,
    create_agent_tool_registry,
    create_data_profile_registry,
)


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


def test_graph_can_choose_real_row_inspection(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [10, 20, 30]}).to_csv(path, index=False)
    registry = create_data_profile_registry([path])
    llm = ScriptedLlm(
        [
            '{"type":"action","thought":"按要求查看第 2 行",'
            '"tool_name":"inspect_data_rows",'
            '"arguments":{"start_row":2,"row_count":1}}',
            '{"type":"finish","thought":"已核实真实记录",'
            '"answer":"第 2 行销量是 20。"}',
        ]
    )
    state = AgentState(user_query="查看第 2 行真实数据", data_paths=[str(path)])

    finish = run_graph(
        state,
        AgentLoop(llm, registry.specs(), "根据指令自主选择工具"),
        Harness(registry),
    )

    assert finish.answer == "第 2 行销量是 20。"
    assert state.observations[0].tool_name == "inspect_data_rows"
    assert state.observations[0].result["rows"] == [
        {"row_number": 2, "values": [20]}
    ]


def test_graph_can_write_code_observe_and_rethink(tmp_path) -> None:
    registry = create_agent_tool_registry([], tmp_path / "output")
    code_action = json.dumps(
        {
            "type": "action",
            "thought": "需要计算验证结果",
            "tool_name": "execute_python",
            "arguments": {"code": "result = {'sum': sum(range(1, 6))}"},
        },
        ensure_ascii=False,
    )
    llm = ScriptedLlm(
        [
            code_action,
            '{"type":"finish","thought":"计算结果已经充分",'
            '"answer":"1 到 5 的和是 15。"}',
        ]
    )
    state = AgentState(user_query="计算 1 到 5 的和")

    finish = run_graph(
        state,
        AgentLoop(llm, registry.specs(), "需要时编写代码验证"),
        Harness(registry),
    )

    assert finish.answer == "1 到 5 的和是 15。"
    assert state.observations[0].tool_name == "execute_python"
    assert state.observations[0].result["result"] == {"sum": 15}
    assert '"sum": 15' in llm.calls[1][-1][1]

def test_graph_generates_artifact_inspects_it_then_finishes(tmp_path) -> None:
    """Agent 应把生成产物的实际内容交回下一轮后再结束。"""
    output_dir = tmp_path / "output"
    registry = create_agent_tool_registry([], output_dir)
    code_action = json.dumps(
        {
            "type": "action",
            "thought": "先生成代码产物",
            "tool_name": "execute_python",
            "arguments": {
                "code": (
                    "path = ARTIFACTS.path('solution.py')\n"
                    "path.write_text('result = 6 * 7\\n', encoding='utf-8')\n"
                    "result = {'created': 'solution.py'}"
                )
            },
        },
        ensure_ascii=False,
    )
    inspect_action = json.dumps(
        {
            "type": "action",
            "thought": "读取实际文件复核",
            "tool_name": "inspect_artifact",
            "arguments": {"relative_path": "solution.py"},
        },
        ensure_ascii=False,
    )
    llm = ScriptedLlm(
        [
            code_action,
            inspect_action,
            '{"type":"finish","thought":"代码内容已复核",'
            '"answer":"代码产物已生成并检查。"}',
        ]
    )
    state = AgentState(user_query="生成计算 6 乘 7 的代码")

    finish = run_graph(
        state,
        AgentLoop(llm, registry.specs(), "生成后必须读取产物复核"),
        Harness(registry),
    )

    assert finish.answer == "代码产物已生成并检查。"
    assert [item.tool_name for item in state.observations] == [
        "execute_python",
        "inspect_artifact",
    ]
    assert "result = 6 * 7" in state.observations[1].result["content"]
    assert "result = 6 * 7" in llm.calls[2][-1][1]
