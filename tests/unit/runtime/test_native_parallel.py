"""原生调用、回退与安全并行的关键回归。"""

import json
import threading
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from data_analysis.agent.graph import iter_graph
from data_analysis.agent.loop import Action, ActionBatch, AgentLoop, DecisionParseError, Finish
from data_analysis.agent.state import AgentState
from data_analysis.observability.client import TracedLlmClient
from data_analysis.observability.trace import RunTrace
from data_analysis.runtime.harness import Harness
from data_analysis.tools.registry import ToolRegistry, ToolSpec


def test_native_stream_keeps_all_calls_and_bound_client_usage():
    class Client:
        def bind_tools(self, tools):
            assert tools[0]["function"]["parameters"]["properties"] == {"x": {"type": "integer"}}
            return self

        def stream(self, messages):
            for index in range(2):
                yield AIMessageChunk(content="", tool_call_chunks=[{
                    "name": "read", "args": '{"x":', "id": f"c{index}", "index": index,
                }])
                yield AIMessageChunk(content="", tool_call_chunks=[{
                    "name": None, "args": f"{index}}}", "id": None, "index": index,
                }])
            yield AIMessageChunk(content="", usage_metadata={"input_tokens": 9, "output_tokens": 3, "total_tokens": 12})

    trace = RunTrace("native")
    loop = AgentLoop(
        TracedLlmClient(Client(), trace),
        [ToolSpec("read", "读取", {"type": "object", "properties": {"x": {"type": "integer"}}})],
        "根据证据分析", tool_mode="auto", progress_callback=lambda *args: None,
    )
    decision = loop.step_batch(AgentState(user_query="读取两份数据"))
    assert isinstance(decision, ActionBatch)
    assert [action.arguments for action in decision.actions] == [{"x": 0}, {"x": 1}]
    assert [action.call_id for action in decision.actions] == ["c0", "c1"]
    assert trace.usage_summary()["input_tokens"] == 9


@pytest.mark.parametrize("malformed", [False, True])
def test_auto_fallback_once_without_executing_partial_calls(malformed):
    class Client:
        bindings = 0

        def bind_tools(self, tools):
            self.bindings += 1
            if not malformed:
                raise NotImplementedError("供应商不支持")
            return SimpleNamespace(invoke=lambda messages: AIMessage(content="", tool_calls=[
                {"name": "invented", "args": {}, "id": "wrong"},
            ]))

        def invoke(self, messages):
            return json.dumps({"type": "action", "tool_name": "read", "arguments": {}})

    client = Client()
    trace = RunTrace("fallback")
    loop = AgentLoop(client, [ToolSpec("read", "读取")], "分析", tool_mode="auto", trace=trace)
    for _ in range(2):
        decision = loop.step_batch(AgentState(user_query="读取"))
        assert isinstance(decision, ActionBatch) and decision.actions[0].tool_name == "read"
    assert client.bindings == 1
    assert len([span for span in trace.spans if span.kind == "fallback"]) == 1


def test_parallel_results_are_ordered_and_trace_parents_are_siblings():
    registry = ToolRegistry()
    barrier = threading.Barrier(2)
    trace = RunTrace("parallel")
    todo = trace.begin("todo", "t1")

    def read(arguments, context):
        barrier.wait(timeout=3)
        with trace.span("llm", "nested"):
            return arguments["index"]

    registry.register_in_process(ToolSpec("read", "读取", mutating=False, parallel_safe=True), read)
    harness = Harness(registry, trace=trace, max_parallel_tools=2)
    results = harness.execute_many([Action(tool_name="read", arguments={"index": i}, call_id=f"c{i}") for i in range(2)])
    assert [result.result for result in results] == [0, 1]
    assert [result.call_id for result in results] == ["c0", "c1"]
    tools = [span for span in trace.spans if span.kind == "tool"]
    assert all(span.parent_span_id == todo.span_id for span in tools)
    assert {span.parent_span_id for span in trace.spans if span.kind == "llm"} == {span.span_id for span in tools}


def test_mutating_or_unknown_tools_keep_entire_batch_serial():
    registry = ToolRegistry()
    executed = []
    registry.register_in_process(ToolSpec("read", "读取", mutating=False, parallel_safe=True), lambda args, ctx: executed.append("read"))
    registry.register_in_process(ToolSpec("write", "写入"), lambda args, ctx: executed.append("write"))
    harness = Harness(registry)
    assert not harness.can_parallel([Action(tool_name="read"), Action(tool_name="write")])
    assert not harness.can_parallel([Action(tool_name="read"), Action(tool_name="missing")])
    results = harness.execute_many([Action(tool_name="write"), Action(tool_name="read")])
    assert all(result.success for result in results)
    assert executed == ["write", "read"]


@pytest.mark.parametrize("parallel", [1, 2])
def test_batch_uses_one_decision_budget_and_preserves_duplicate_observation(parallel):
    registry = ToolRegistry()
    registry.register_in_process(ToolSpec("read", "读取", mutating=False, parallel_safe=True), lambda args, ctx: args["x"])
    actions = [Action(tool_name="read", arguments={"x": i}, call_id=f"c{i}") for i in (0, 1, 0)]
    decisions = iter([ActionBatch(actions=actions), Finish(answer="完成")])
    state = AgentState(user_query="读取")
    trace = RunTrace("batch")
    events = list(iter_graph(state, SimpleNamespace(step_batch=lambda state: next(decisions)), Harness(registry, trace=trace, max_parallel_tools=parallel)))
    assert isinstance(events[-1], Finish)
    assert state.iteration == state.run_budget.iterations == 1
    assert len(state.observations) == 3
    assert state.observations[-1].success is False
    assert len([span for span in trace.spans if span.kind == "tool"]) == 3


def test_malformed_native_batches_are_rejected_before_execution():
    from data_analysis.agent.decision import parse_native_response

    for calls in [
        [{"name": [], "args": {}}],
        [{"name": "read", "args": "not an object"}],
        [{"name": "read", "args": {"x": float("nan")}}],
        [{"name": "read", "args": {}, "id": "same"}] * 2,
    ]:
        with pytest.raises(DecisionParseError):
            parse_native_response(SimpleNamespace(tool_calls=calls), "", frozenset({"read"}))
    partial = AIMessageChunk(content="", tool_call_chunks=[
        {"name": "read", "args": '{"x":1', "id": "cut", "index": 0},
    ])
    with pytest.raises(DecisionParseError):
        parse_native_response(partial, "", frozenset({"read"}))
