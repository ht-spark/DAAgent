"""Agent 流式接口的计量持久化与审批追踪回归测试。"""

import json
import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from data_analysis.experts import ExpertRegistry
from data_analysis.skills import SkillRegistry
from data_analysis.tools.registry import ToolRegistry, ToolSpec
from server import main, store


class ScriptedClient:
    def __init__(self, decisions):
        self.decisions = iter(decisions)

    def invoke(self, messages):
        decision = next(self.decisions)
        if isinstance(decision, Exception):
            raise decision
        return SimpleNamespace(
            content=json.dumps(decision, ensure_ascii=False),
            usage_metadata={"input_tokens": 10, "output_tokens": 2},
        )


@pytest.fixture
def api_context(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "agent.db")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(main, "AGENT_TRACE_ROOT", tmp_path / "traces")
    monkeypatch.setattr(main, "_read_api_settings", lambda: {})
    monkeypatch.setattr(main, "_agent_context_window_tokens", lambda config: 100_000)
    monkeypatch.setattr(main, "resolve_python_environment", lambda root: SimpleNamespace(
        executable=sys.executable, source="test", name="mock",
    ))
    monkeypatch.setattr(main, "_agent_chat_runs", {})
    # 既有计量用例只覆盖原调度；独立验证在专门集成用例中启用。
    monkeypatch.setattr(main, "Verifier", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "FINAL_ANSWER_CHUNK_DELAY_SECONDS", 0)
    monkeypatch.setattr(main, "AGENT_COMMAND_APPROVAL_TIMEOUT_SECONDS", 0.01)
    skills = SkillRegistry(tmp_path / "skills")
    monkeypatch.setattr(main, "_skill_registry", skills)
    monkeypatch.setattr(main, "_expert_registry", ExpertRegistry(tmp_path / "experts", skills))
    monkeypatch.delenv("AGENT_LLM_INPUT_COST_PER_MILLION", raising=False)
    monkeypatch.delenv("AGENT_LLM_OUTPUT_COST_PER_MILLION", raising=False)
    registry = ToolRegistry()
    registry.register_in_process(
        ToolSpec("run_terminal_command", "执行命令"),
        lambda arguments, context: {"exit_code": 0, "output": "已完成"},
    )
    monkeypatch.setattr(main, "create_agent_tool_registry", lambda *args, **kwargs: registry)
    yield tmp_path
    if store._conn is not None:
        store._conn.close()


async def _start(monkeypatch, decisions):
    monkeypatch.setattr(main, "_discussion_llm_by_id", lambda *args: ScriptedClient(decisions))
    return await main.agent_profile_stream_endpoint(
        message="分析数据", discussion_id=None, title=None, files=None, config_id=None,
        reasoning_effort=None, knowledge_base_id=None, knowledge_base_ids=None,
        skill_ids=None, expert_id=None, project_id=None,
    )


@pytest.mark.asyncio
async def test_paused_approval_survives_run_reconstruction(api_context, monkeypatch):
    from server.checkpoint_store import load_checkpoint

    monkeypatch.setattr(main, "AGENT_COMMAND_APPROVAL_TIMEOUT_SECONDS", 5)
    response = await _start(monkeypatch, [
        {"objective": "检查", "todos": [{"id": "t", "objective": "执行", "expected_outputs": ["证据"]}]},
        {"type": "action", "tool_name": "run_terminal_command", "arguments": {"command": "测试命令"}},
    ])
    run_id, approval_id = None, None
    async for line in response.body_iterator:
        event = json.loads(line)
        if event["type"] == "agent_started":
            run_id = event["run_id"]
        if event["type"] == "agent_command_approval_required":
            approval_id = event["approval_id"]
            await main.pause_agent_chat_run(run_id)
    assert load_checkpoint(run_id)["status"] == "paused"
    session_id = load_checkpoint(run_id)["session_id"]
    assert store.get_session(session_id)["messages"] == []
    monkeypatch.setattr(main, "_agent_chat_runs", {})
    monkeypatch.setattr(main, "_discussion_llm_by_id", lambda *args: ScriptedClient([
        {"type": "finish", "answer": "操作完成"}, {"type": "finish", "answer": "最终交付"},
    ]))
    response = await main.resume_agent_chat_run(run_id)
    events = []
    async for line in response.body_iterator:
        event = json.loads(line)
        events.append(event)
        if event["type"] == "agent_command_approval_required":
            assert event["approval_id"] == approval_id
            await main.decide_agent_command_endpoint(approval_id, main.AgentCommandDecisionBody(approved=True))
    assert events[-1]["type"] == "done"
    assert events[-1]["run_id"] == run_id
    assert events[-1]["usage_summary"]["input_tokens"] == 40
    record = load_checkpoint(run_id)
    assert record["status"] == "completed" and not record["resumable"]
    assert sum(step["kind"] == "harness.execute" for step in main.SqliteCheckpointStore(run_id).steps()) == 1
    assert len(store.get_session(session_id)["messages"]) == 2


@pytest.mark.asyncio
async def test_verification_is_visible_traced_and_persisted(api_context, monkeypatch):
    from data_analysis.agent.verifier import Verifier

    monkeypatch.setattr(main, "Verifier", Verifier)
    response = await _start(monkeypatch, [
        {"type": "finish", "answer": "文件已生成：[报告](missing.md)"},
        {"type": "finish", "answer": "尚未生成报告，无法交付该文件。"},
        {"status": "pass", "issues": [], "checked_artifacts": []},
    ])
    events = [json.loads(line) async for line in response.body_iterator]
    checks = [event for event in events if event.get("verification")]
    assert [event["verification"]["status"] for event in checks] == ["fail", "pass"]
    assert all(event["type"] == "agent_reasoning" for event in checks)
    answer = "".join(event["content"] for event in events if event["type"] == "token")
    assert "missing.md" not in answer and "尚未生成" in answer
    done = events[-1]
    assert done["verification_summary"]["status"] == "pass"
    assert done["verification_summary"]["repairs"] == 1
    metadata = store.get_session(done["discussion_id"])["messages"][-1]["metadata"]
    assert metadata["verification_summary"] == done["verification_summary"]
    spans = store.list_agent_spans(done["run_id"])
    assert sum(span["kind"] == "verify" for span in spans) == 2
    assert done["usage_summary"]["input_tokens"] == 30


@pytest.mark.asyncio
async def test_done_and_saved_message_share_trace_and_usage(api_context, monkeypatch):
    response = await _start(monkeypatch, [{"type": "finish", "answer": "已回答"}])
    events = [json.loads(line) async for line in response.body_iterator]
    done = events[-1]
    assert done["type"] == "done"
    assert done["trace_id"]
    assert done["usage_summary"]["input_tokens"] == 10
    assert done["usage_summary"]["cost_estimate"] is None
    rows = store.list_agent_spans(done["run_id"])
    assert {row["kind"] for row in rows} == {"run", "plan", "llm"}
    assert all(row["status"] == "ok" and row["trace_id"] == done["trace_id"] for row in rows)
    metadata = store.get_session(done["discussion_id"])["messages"][-1]["metadata"]
    assert metadata["trace_id"] == done["trace_id"]
    assert metadata["usage_summary"] == done["usage_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["approved", "denied", "timeout", "cancelled"])
async def test_approval_wait_is_traced_for_all_outcomes(api_context, monkeypatch, outcome):
    finish = {"type": "finish", "answer": "结束"}
    response = await _start(monkeypatch, [
        {"objective": "检查", "todos": [{"id": "t1", "objective": "检查", "expected_outputs": ["证据"]}]},
        {"type": "action", "tool_name": "run_terminal_command", "arguments": {"command": "无实际命令"}},
        finish, finish,
    ])
    events = []
    async for line in response.body_iterator:
        event = json.loads(line)
        events.append(event)
        if event["type"] == "agent_command_approval_required" and outcome != "timeout":
            if outcome == "cancelled":
                next(iter(main._agent_chat_runs.values())).cancel_requested = True
            main._pending_agent_commands[event["approval_id"]].set_result(outcome == "approved")
    started = events[0]
    spans = store.list_agent_spans(started["run_id"])
    approval = next(span for span in spans if span["kind"] == "approval")
    assert next(span for span in spans if span["span_id"] == approval["parent_span_id"])["kind"] == "todo"
    assert approval["status"] == {"approved": "ok", "denied": "error", "timeout": "error", "cancelled": "cancelled"}[outcome]
    if outcome == "timeout":
        assert approval["error_type"] == "ApprovalTimeout"
    if outcome == "cancelled":
        assert events[-1]["type"] == "cancelled"
        assert events[-1]["usage_summary"]["input_tokens"] == 20
        assert events[-1]["run_id"] == started["run_id"]
        assert next(span for span in spans if span["kind"] == "run")["status"] == "cancelled"
    else:
        observation = next(event for event in events if event["type"] == "agent_observation")
        assert observation["success"] is (outcome == "approved")
        assert observation["span_id"]


@pytest.mark.asyncio
async def test_failed_model_call_exports_error_trace(api_context, monkeypatch):
    response = await _start(monkeypatch, [RuntimeError("不可记录的密钥")])
    events = [json.loads(line) async for line in response.body_iterator]
    assert events[-1]["type"] == "error"
    assert events[-1]["usage_summary"]["unknown_usage_calls"] == 1
    rows = store.list_agent_spans(events[0]["run_id"])
    assert all(row["status"] == "error" for row in rows)
    assert "不可记录的密钥" not in json.dumps(rows, ensure_ascii=False)


def test_optional_pricing_configuration(api_context, monkeypatch):
    assert main._agent_token_pricing() is None
    monkeypatch.setenv("AGENT_LLM_INPUT_COST_PER_MILLION", "1")
    monkeypatch.setenv("AGENT_LLM_OUTPUT_COST_PER_MILLION", "2")
    assert main._agent_token_pricing().estimate(100, 50) == pytest.approx(0.0002)
    monkeypatch.setenv("AGENT_LLM_INPUT_COST_PER_MILLION", "nan")
    assert main._agent_token_pricing() is None


@pytest.mark.asyncio
async def test_native_batch_stream_preserves_parallel_reads_and_each_terminal_approval(api_context, monkeypatch):
    """真实服务装配走原生批次，安全查询并行，两个终端调用分别审批。"""
    finish = {"type": "finish", "answer": "完成"}
    responses = iter([
        AIMessage(content="", tool_calls=[
            {"name": "read", "args": {"index": i}, "id": f"read-{i}"} for i in range(2)
        ], usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}),
        AIMessage(content="", tool_calls=[
            {"name": "run_terminal_command", "args": {"command": str(i)}, "id": f"terminal-{i}"} for i in range(2)
        ], usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}),
        AIMessage(content=json.dumps(finish), usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}),
        AIMessage(content=json.dumps(finish), usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}),
    ])

    class Client:
        def invoke(self, messages):
            return AIMessage(content=json.dumps({"objective": "核验", "todos": [
                {"id": "t1", "objective": "核验", "expected_outputs": ["证据"]},
            ]}), usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})

        def bind_tools(self, tools):
            return SimpleNamespace(invoke=lambda messages: next(responses))

    barrier = threading.Barrier(2)

    def read(arguments, context):
        barrier.wait(timeout=3)
        return arguments["index"]

    registry = ToolRegistry()
    registry.register_in_process(ToolSpec("read", "读取", mutating=False, parallel_safe=True), read)
    commands = []
    registry.register_in_process(ToolSpec("run_terminal_command", "执行"), lambda args, ctx: commands.append(args["command"]))
    monkeypatch.setenv("AGENT_TOOL_MODE", "auto")
    monkeypatch.setenv("AGENT_MAX_PARALLEL_TOOLS", "2")
    monkeypatch.setattr(main, "create_agent_tool_registry", lambda *args, **kwargs: registry)
    monkeypatch.setattr(main, "_discussion_llm_by_id", lambda *args: Client())
    response = await main.agent_profile_stream_endpoint(
        message="核验", discussion_id=None, title=None, files=None, config_id=None,
        reasoning_effort=None, knowledge_base_id=None, knowledge_base_ids=None,
        skill_ids=None, expert_id=None, project_id=None,
    )
    events = []
    approvals = []
    async for line in response.body_iterator:
        event = json.loads(line)
        events.append(event)
        if event["type"] == "agent_command_approval_required":
            approvals.append(event["approval_id"])
            main._pending_agent_commands[event["approval_id"]].set_result(True)
    assert len(approvals) == 2 and commands == ["0", "1"]
    assert events[-1]["type"] == "done"
    assert events[-1]["usage_summary"]["input_tokens"] == 50
    observations = [event for event in events if event["type"] == "agent_observation"]
    assert [event["call_id"] for event in observations] == ["read-0", "read-1", "terminal-0", "terminal-1"]
    assert all(event["success"] for event in observations)


@pytest.mark.asyncio
async def test_successful_review_updates_plan_snapshot_and_saved_counters(api_context, monkeypatch):
    """服务默认装配能在成功 Todo 后复审，历史与完成事件保留同一计数。"""
    registry = ToolRegistry()
    registry.register_in_process(ToolSpec("read", "读取"), lambda args, ctx: {"rows": 7, "columns": ["actual"]})
    monkeypatch.setattr(main, "create_agent_tool_registry", lambda *args, **kwargs: registry)
    monkeypatch.setenv("AGENT_ADAPTIVE_REPLAN_ENABLED", "true")
    monkeypatch.setenv("AGENT_REPLAN_CHECK_INTERVAL", "0")
    monkeypatch.setenv("AGENT_MAX_ADAPTIVE_REPLANS", "2")
    monkeypatch.setenv("AGENT_MAX_PLAN_REVIEWS", "6")
    finish = {"type": "finish", "answer": "依据实际字段完成核验"}
    response = await _start(monkeypatch, [
        {"objective": "核验", "todos": [
            {"id": "read", "objective": "读取", "expected_outputs": ["证据"]},
            {"id": "old", "objective": "旧假设", "expected_outputs": ["核验"]},
        ]},
        {"type": "action", "tool_name": "read", "arguments": {}},
        finish,
        {"action": "revise", "reasoning_summary": "实际字段不同，更新剩余核验", "todos": [
            {"id": "actual", "objective": "核验实际字段", "expected_outputs": ["真实证据"], "depends_on": ["read"]},
        ]},
        finish, finish,
    ])
    events = [json.loads(line) async for line in response.body_iterator]
    plans = [event for event in events if event["type"] == "agent_plan"]
    assert len(plans) == 2 and plans[-1]["revision_kind"] == "adaptive"
    assert plans[-1]["todos"][0]["status"] == "completed"
    assert plans[-1]["todos"][1]["id"] == "adaptive_1_actual"
    done = events[-1]
    assert done["type"] == "done"
    assert done["replanning_summary"] == {"reviews": 1, "adaptive_replans": 1, "failure_replans": 0}
    metadata = store.get_session(done["discussion_id"])["messages"][-1]["metadata"]
    assert metadata["replanning_summary"] == done["replanning_summary"]
    assert any(span["name"] == "reassess_todo_completed" for span in store.list_agent_spans(done["run_id"]))
