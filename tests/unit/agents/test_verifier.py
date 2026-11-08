"""独立复核、产物边界和有限修订的关键回归。"""

import json
from types import SimpleNamespace

import pytest

from data_analysis.agent.graph import iter_plan_graph
from data_analysis.agent.loop import Action, Finish, IterationLimitError
from data_analysis.agent.state import AgentState, AnalysisPlan, Observation, discover_artifacts
from data_analysis.agent.verifier import Verification, Verifier
from data_analysis.cancellation import TaskCancelledError
from data_analysis.observability.trace import RunTrace


def _client(*responses):
    values = iter(responses)
    return SimpleNamespace(invoke=lambda messages: next(values))


def _verdict(status="pass"):
    return json.dumps({"status": status, "issues": [] if status == "pass" else [{
        "code": "conflicting_value", "message": "总数应引用工具实际结果。",
        "evidence": "read_total.result.total=12，与候选回答的 20 冲突。",
    }], "checked_artifacts": []})


def test_artifact_checks_do_not_read_outside_root_and_work_when_semantic_disabled(tmp_path):
    state = AgentState(user_query="交付文件", output_dir=str(tmp_path / "outputs"))
    root = tmp_path / "outputs"
    root.mkdir()
    outside = tmp_path / "private.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    (root / "bad.json").write_text("{", encoding="utf-8")
    (root / "valid file.json").write_text("{}", encoding="utf-8")
    result = Verifier(semantic_enabled=False).verify(state, None,
        "[报告](missing.md) [越界](../private.json) [数据](/api/agent/discussions/x/files/bad.json) "
        "[有效](<valid file.json>) [网页](https://example.com) [锚点](#section)")
    assert result.status == "fail"
    assert len(result.issues) == 3
    assert "../private.json" not in result.checked_artifacts
    assert "valid file.json" in result.checked_artifacts
    assert "secret" not in result.model_dump_json()
    assert Verifier(semantic_enabled=False).verify(state, None, "你好").status == "uncertain"


@pytest.mark.parametrize("response", ["坏 JSON", '{"status":"fail","issues":[]}',
                                       '{"status":"pass","issues":[],"extra":true}'])
def test_malformed_verdict_degrades_to_uncertain(response):
    result = Verifier(_client(response)).verify(AgentState(user_query="分析"), None, "候选")
    assert result.status == "uncertain"
    assert result.issues[0].code == "verifier_unavailable"


@pytest.mark.parametrize("direct", [True, False])
def test_repair_uses_real_tools_and_commits_only_verified_answer(direct):
    state = AgentState(user_query="计算总数")
    state.absorb_observation(Observation(tool_name="read_total", success=True, result={"total": 12}))
    wrong = Finish(answer="总数为 20")
    plan = AnalysisPlan(objective="计算", todos=[{"id": "t", "objective": "读取", "expected_outputs": ["总数"]}])
    planner = SimpleNamespace(create_plan=lambda state: wrong if direct else plan)
    choices = iter(([Finish(answer="读到12"), wrong] if not direct else []) + [
        Action(tool_name="read_total", arguments={}), Finish(answer="总数为 12"),
    ])
    def step(current):
        assert not state.messages
        decision = next(choices)
        if isinstance(decision, Action):
            assert "conflicting_value" in current.recent_observations[-1].model_dump_json()
        return decision
    loop = SimpleNamespace(step=step)
    harness = SimpleNamespace(execute=lambda action: Observation(tool_name=action.tool_name, success=True, result={"total": 12}))
    captured = []
    client = _client(_verdict("fail"), _verdict())
    invoke = client.invoke
    client.invoke = lambda messages: (captured.append(messages) or invoke(messages))
    trace = RunTrace("verification-test")
    events = list(iter_plan_graph(state, planner, loop, harness,
                                 verifier=Verifier(client, trace=trace), trace=trace))
    assert [item.status for item in events if isinstance(item, Verification)] == ["fail", "pass"]
    assert events[-1].answer == "总数为 12"
    assert len(state.messages) == 2 and state.messages[-1].content == "总数为 12"
    assert state.run_budget.iterations == 1
    assert state.verification_summary["repairs"] == 1
    assert '"total": 12' in captured[0][-1][1].replace('\\"', '"')


def test_failed_repair_is_bounded_and_explicitly_disclosed():
    state = AgentState(user_query="分析")
    answer = Finish(answer="仍待核验的候选")
    planner = SimpleNamespace(create_plan=lambda state: answer)
    calls = []
    loop = SimpleNamespace(step=lambda state: (calls.append(state) or answer))
    events = list(iter_plan_graph(state, planner, loop, SimpleNamespace(),
        verifier=Verifier(_client(_verdict("fail"), _verdict("fail")))))
    assert len(calls) == 1
    assert state.verification_summary["status"] == "uncertain"
    assert "未完全通过" in events[-1].answer
    assert "总数" in events[-1].answer


def test_verification_cancellation_is_not_downgraded():
    with pytest.raises(TaskCancelledError):
        Verifier(_client(_verdict()), cancel_check=lambda: True).verify(
            AgentState(user_query="分析"), None, "候选")


def test_explaining_a_missing_file_does_not_count_as_delivering_it(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("旧报告", encoding="utf-8")
    state = AgentState(user_query="报告", output_dir=str(tmp_path), available_artifacts=discover_artifacts(tmp_path))
    path.unlink()
    result = Verifier(_client(_verdict())).verify(state, None, "report.md 已删除，无法交付。")
    assert result.status == "pass"


def test_exhausted_run_budget_does_not_reset_or_hide_failed_verification():
    state = AgentState(user_query="分析")
    state.run_budget.iterations = 50
    def exhausted(current):
        assert current.run_budget is state.run_budget
        raise IterationLimitError("预算用尽")
    events = list(iter_plan_graph(state, SimpleNamespace(create_plan=lambda state: Finish(answer="候选")),
        SimpleNamespace(step=exhausted), SimpleNamespace(), verifier=Verifier(_client(_verdict("fail")))))
    assert state.run_budget.iterations == 50
    assert state.verification_summary["status"] == "uncertain"
    assert state.verification_summary["repairs"] == 1
    assert "未完全通过" in events[-1].answer
