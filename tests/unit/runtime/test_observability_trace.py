"""真实调度器的追踪树、双写和中断验收测试。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk

from data_analysis.agent.graph import iter_plan_graph
from data_analysis.agent.loop import Action, AgentLoop, Finish, invoke_llm_with_public_progress
from data_analysis.agent.planner import Planner
from data_analysis.agent.state import AgentState, Observation
from data_analysis.cancellation import TaskCancelledError
from data_analysis.observability.client import TracedLlmClient
from data_analysis.observability.exporter import LocalTraceExporter
from data_analysis.observability.trace import RunTrace
from data_analysis.observability.usage import TokenPricing
from data_analysis.runtime.harness import Harness
from data_analysis.skills.models import SkillAction, SkillObservation
from data_analysis.tools.registry import ToolRegistry, ToolSpec
from server import store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "agent.db")
    monkeypatch.setattr(store, "_conn", None)
    yield
    if store._conn is not None:
        store._conn.close()


def _plan():
    return {"objective": "核验数据", "todos": [
        {"id": f"t{i}", "objective": f"核验 {i}", "expected_outputs": ["证据"]}
        for i in range(3)
    ]}


def _finish():
    return {"type": "finish", "answer": "已核验"}


class ScriptedClient:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = 0

    def stream(self, messages):
        self.calls += 1
        yield AIMessageChunk(content=json.dumps(next(self.decisions), ensure_ascii=False))
        yield AIMessageChunk(content="", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})


@pytest.mark.asyncio
async def test_three_todos_five_tools_export_complete_tree_across_threads(tmp_path, isolated_store):
    exporter = LocalTraceExporter(tmp_path / "traces", "acceptance", store.save_agent_span)
    trace = RunTrace("acceptance", exporter)
    decisions = [_plan()]
    for count in (2, 2, 1):
        decisions.extend({"type": "action", "tool_name": "read", "arguments": {"offset": i}} for i in range(count))
        decisions.append(_finish())
    decisions.append(_finish())
    raw_client = ScriptedClient(decisions)
    client = TracedLlmClient(raw_client, trace, pricing=TokenPricing(input_per_million=1, output_per_million=2))
    registry = ToolRegistry()
    registry.register_in_process(ToolSpec("read", "读取证据"), lambda arguments, context: {"value": 1})
    callback = lambda *args: None
    loop = AgentLoop(client, registry.specs(), "根据证据分析", progress_callback=callback, cancel_check=lambda: False)
    planner = Planner(client, registry.specs(), progress_callback=callback, cancel_check=lambda: False)
    harness = Harness(registry, trace=trace)
    iterator = iter_plan_graph(AgentState(user_query="核验数据"), planner, loop, harness, trace=trace)
    events = []
    sentinel = object()
    while True:
        event = await asyncio.to_thread(next, iterator, sentinel)
        if event is sentinel:
            break
        events.append(event)
    trace.finish()

    spans = trace.spans
    roots = [span for span in spans if span.kind == "run"]
    assert len(roots) == 1 and roots[0].parent_span_id is None
    assert len([span for span in spans if span.kind == "todo"]) == 3
    assert len([span for span in spans if span.kind == "llm"]) == raw_client.calls == 10
    observations = [event for event in events if isinstance(event, Observation)]
    tools = [span for span in spans if span.kind == "tool"]
    assert len(tools) == len(observations) == 5
    assert {event.span_id for event in observations} == {span.span_id for span in tools}
    by_id = {span.span_id: span for span in spans}
    for span in spans:
        assert span.run_id == "acceptance" and span.trace_id == trace.trace_id
        if span.parent_span_id:
            assert span.parent_span_id in by_id
            parent_kind = by_id[span.parent_span_id].kind
            if span.kind == "tool":
                assert parent_kind == "todo"
            elif span.kind in {"todo", "plan"}:
                assert parent_kind == "run"
            else:
                assert parent_kind in {"run", "plan", "todo"}
        assert span.status == "ok"
    assert trace.usage_summary()["input_tokens"] == 100
    assert trace.usage_summary()["output_tokens"] == 20
    assert trace.usage_summary()["cost_estimate"] == pytest.approx(0.00014)
    assert [item["input_tokens"] for item in trace.usage_summary()["by_todo"]] == [30, 30, 20]
    jsonl = [json.loads(line) for line in exporter.path.read_text(encoding="utf-8").splitlines()]
    rows = store.list_agent_spans("acceptance")
    assert {item["span_id"] for item in jsonl} == {item["span_id"] for item in rows} == set(by_id)
    assert {item["span_id"]: item for item in jsonl} == {item["span_id"]: item for item in rows}


def test_repeated_tool_rejection_still_has_one_span_per_observation():
    decisions = [_plan()]
    for i in range(3):
        decisions.extend([
            {"type": "action", "tool_name": "read", "arguments": {}},
            {"type": "action", "tool_name": "read", "arguments": {}},
            _finish(),
        ])
    decisions.append(_finish())
    trace = RunTrace("repeat")
    client = TracedLlmClient(ScriptedClient(decisions), trace)
    registry = ToolRegistry()
    registry.register_in_process(ToolSpec("read", "读取"), lambda arguments, context: 1)
    callback = lambda *args: None
    events = list(iter_plan_graph(
        AgentState(user_query="读取"), Planner(client, registry.specs(), progress_callback=callback),
        AgentLoop(client, registry.specs(), "分析", progress_callback=callback),
        Harness(registry, trace=trace), trace=trace,
    ))
    trace.finish()
    observations = [event for event in events if isinstance(event, Observation)]
    tools = [span for span in trace.spans if span.kind == "tool"]
    assert len(tools) == len(observations) == 6
    assert len([span for span in tools if span.error_type == "RepeatedToolCall"]) == 3
    assert all(observation.span_id for observation in observations)


def test_cancellation_seals_active_spans_and_ignores_late_responses():
    trace = RunTrace("cancel")
    todo = trace.begin("todo", "t1")
    llm = trace.begin("llm", "mock")
    trace.finish("cancelled", "TaskCancelledError")
    trace.end(llm, input_tokens=10, output_tokens=2)
    assert all(span.status == "cancelled" for span in trace.spans)
    assert llm.input_tokens is None and todo.input_tokens is None
    assert trace.begin("tool", "late") is None


def test_stream_error_and_explicit_close_keep_status():
    class Client:
        def stream(self, messages):
            yield SimpleNamespace(content="半条", usage_metadata=None)
            raise RuntimeError("敏感信息")

    trace = RunTrace("stream-error")
    client = TracedLlmClient(Client(), trace)
    with pytest.raises(RuntimeError):
        list(client.stream([]))
    iterator = client.stream([])
    next(iterator)
    iterator.close()
    assert [(span.status, span.error_type) for span in trace.spans if span.kind == "llm"] == [
        ("error", "RuntimeError"), ("cancelled", "GeneratorExit"),
    ]


def test_empty_stream_keeps_original_compatibility_fallback():
    class Client:
        disable_streaming = False

        def stream(self, messages):
            yield SimpleNamespace(content="", usage_metadata=None)

        def invoke(self, messages):
            assert self.disable_streaming
            return "正文"

    trace = RunTrace("empty-stream")
    _, text = invoke_llm_with_public_progress(
        TracedLlmClient(Client(), trace), [], summary_key="thought", stream_id="s",
        initial_text="分析", progress_callback=lambda *args: None, cancel_check=None,
    )
    assert text == "正文"
    assert len([span for span in trace.spans if span.kind == "llm"]) == 2


def test_export_channels_fail_independently(tmp_path):
    written = []
    blocked_root = tmp_path / "file"
    blocked_root.write_text("阻止建目录", encoding="utf-8")
    trace = RunTrace("write-error", LocalTraceExporter(blocked_root / "child", "write-error", written.append))
    trace.finish()
    assert len(written) == 1
    exporter = LocalTraceExporter(tmp_path / "ok", "db-error", lambda payload: (_ for _ in ()).throw(RuntimeError("数据库故障")))
    RunTrace("db-error", exporter).finish()
    assert len(exporter.path.read_text(encoding="utf-8").splitlines()) == 1
    with pytest.raises(ValueError):
        LocalTraceExporter(tmp_path, "../escape", written.append)


def test_harness_failure_and_cancellation_are_traced():
    trace = RunTrace("tools")
    harness = Harness(ToolRegistry(), trace=trace)
    result = harness.execute(Action(tool_name="missing"))
    assert result.success is False and result.span_id is not None
    assert trace.spans[-1].status == "error"
    harness = Harness(ToolRegistry(), trace=trace, cancel_check=lambda: True)
    with pytest.raises(TaskCancelledError):
        harness.execute(Action(tool_name="missing"))
    assert trace.spans[-1].status == "cancelled"


def test_recovery_plan_and_failed_todo_keep_separate_spans():
    plan = {"objective": "检查", "todos": [{"id": "t1", "objective": "检查", "expected_outputs": ["证据"]}]}
    action = {"type": "action", "tool_name": "read", "arguments": {}}
    trace = RunTrace("recover")
    client = TracedLlmClient(ScriptedClient([plan, action, action, action, plan, _finish(), _finish()]), trace)
    registry = ToolRegistry()

    def fail(arguments, context):
        raise ValueError("读取失败")

    registry.register_in_process(ToolSpec("read", "读取"), fail)
    callback = lambda *args: None
    list(iter_plan_graph(
        AgentState(user_query="检查"), Planner(client, registry.specs(), progress_callback=callback),
        AgentLoop(client, registry.specs(), "检查", progress_callback=callback),
        Harness(registry, trace=trace), trace=trace,
    ))
    trace.finish()
    assert [span.name for span in trace.spans if span.kind == "plan"] == ["create_plan", "recovery_plan"]
    assert [span.status for span in trace.spans if span.kind == "todo"] == ["error", "ok"]
    plans = {span.span_id for span in trace.spans if span.kind == "plan"}
    assert len([span for span in trace.spans if span.kind == "llm" and span.parent_span_id in plans]) == 2


def test_skill_operation_is_child_of_active_todo():
    class PlannerStub:
        def create_plan(self, state):
            from data_analysis.agent.state import AnalysisPlan

            return AnalysisPlan.model_validate({"objective": "读取技能", "todos": [
                {"id": "t1", "objective": "读取", "expected_outputs": ["证据"]},
            ]})

    decisions = iter([
        SkillAction(operation="read_reference", skill_id="example", resource_id="ref"),
        Finish(answer="已读"), Finish(answer="完成"),
    ])
    runtime = SimpleNamespace(execute=lambda action: SkillObservation(
        operation=action.operation, skill_id=action.skill_id, success=True,
    ))
    trace = RunTrace("skill")
    list(iter_plan_graph(
        AgentState(user_query="读取"), PlannerStub(), SimpleNamespace(step=lambda state: next(decisions)),
        Harness(ToolRegistry(), trace=trace), skill_runtime=runtime, trace=trace,
    ))
    trace.finish()
    skill = next(span for span in trace.spans if span.kind == "skill")
    todo = next(span for span in trace.spans if span.kind == "todo")
    assert skill.parent_span_id == todo.span_id and skill.status == "ok"
