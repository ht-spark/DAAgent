"""检查点重演的预算、执行去重和不明副作用边界。"""

from types import SimpleNamespace

import pytest

from data_analysis.agent.graph import iter_plan_graph
from data_analysis.agent.loop import Action, Finish
from data_analysis.agent.state import AgentState, AnalysisPlan, Observation
from data_analysis.runtime.checkpoint import CheckpointJournal
from data_analysis.observability.trace import RunTrace
from server import store
from server.checkpoint_store import SqliteCheckpointStore, load_checkpoint


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "checkpoint.db")
    store.create_session("测试", "session")
    adapter = SqliteCheckpointStore("run")
    adapter.create("session", {}, {}, "trace")
    yield adapter
    store._conn.close()


def test_resume_replays_observations_without_reexecuting_and_shares_one_budget(checkpoint):
    plan = AnalysisPlan(objective="检查并交付", todos=[
        {"id": name, "objective": name, "expected_outputs": ["证据"]} for name in ("write", "read")])
    initial = AgentState(user_query="分析").model_dump(mode="json")
    called = []
    def execute(action):
        called.append(action.tool_name)
        return Observation(tool_name=action.tool_name, success=True, result={"value": 12})
    harness = SimpleNamespace(execute=execute)
    events = iter_plan_graph(AgentState.model_validate(initial),
        SimpleNamespace(create_plan=lambda state: plan),
        SimpleNamespace(step=lambda state: Action(tool_name="write", arguments={})), harness,
        checkpoint=CheckpointJournal(checkpoint))
    while not isinstance(next(events), Observation):
        pass
    events.close()
    checkpoint.finish("paused")
    restored = SqliteCheckpointStore("run", "resumed")
    restored.claim()
    state = AgentState.model_validate(initial)
    decisions = iter([Finish(answer="已写入"), Action(tool_name="read", arguments={}),
                      Finish(answer="已读取"), Finish(answer="交付")])
    def step(current):
        assert current.run_budget is state.run_budget
        return next(decisions)
    def unexpected(current):
        raise AssertionError("不得重新调用已完成的 Planner")
    result = list(iter_plan_graph(state, SimpleNamespace(create_plan=unexpected),
        SimpleNamespace(step=step), harness, checkpoint=CheckpointJournal(restored)))
    assert result[-1].answer == "交付"
    assert called == ["write", "read"]
    assert state.run_budget.iterations == 2
    assert [item.tool_name for item in state.recent_observations] == ["write", "read"]


def test_uncommitted_tool_result_is_not_automatically_executed_again(checkpoint):
    action = Action(tool_name="run_terminal_command", arguments={"command": "write"})
    checkpoint.begin("harness.execute", action.model_dump(mode="json"))
    checkpoint.finish("paused")
    restored = SqliteCheckpointStore("run", "new")
    restored.claim()
    def unexpected(action):
        raise AssertionError("状态不明的操作不得自动重跑")
    harness = CheckpointJournal(restored).wrap(SimpleNamespace(execute=unexpected), "harness", ("execute",))
    result = harness.execute(action)
    assert not result.success and "外部影响未知" in result.error
    assert restored.steps()[0]["completed"]


def test_lease_claim_and_schema_version_prevent_duplicate_resume(checkpoint):
    checkpoint.finish("paused")
    SqliteCheckpointStore("run", "winner").claim()
    with pytest.raises(ValueError):
        SqliteCheckpointStore("run", "second").claim()
    with pytest.raises(RuntimeError):
        checkpoint.begin("harness.execute", {})
    SqliteCheckpointStore("run", "winner").finish("paused")
    with store._lock:
        conn = store._connect()
        conn.execute("UPDATE agent_checkpoints SET schema_version=99")
        conn.commit()
    assert not load_checkpoint("run")["resumable"]


def test_interrupted_usage_preserves_known_subtotal_without_claiming_complete_total():
    trace = RunTrace("run", trace_id="stable", interrupted_usage=True)
    span = trace.begin("llm", "after_resume")
    trace.end(span, input_tokens=10, output_tokens=2)
    summary = trace.usage_summary()
    assert summary["known_input_tokens"] == 10
    assert summary["input_tokens"] is None and summary["output_tokens"] is None
    assert summary["interrupted_usage"]
