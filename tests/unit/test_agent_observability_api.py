"""Agent 流式接口的计量持久化与审批追踪回归测试。"""

import json
import sys
from types import SimpleNamespace

import pytest

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
