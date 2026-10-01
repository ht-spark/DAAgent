from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from data_analysis.agent.loop import AgentLoop
from data_analysis.agent.state import (
    MAX_TRAJECTORY_DECISIONS,
    MAX_TRAJECTORY_PATHS,
    AgentMessage,
    AgentState,
    ArtifactReference,
    Observation,
    RunBudget,
    WorkTrajectory,
    clip_text,
    discover_artifacts,
)


class FakeLlm:
    def invoke(self, messages: list[tuple[str, str]]) -> str:
        return '{"type":"finish","answer":"完成"}'


def test_state_defaults_match_agent_loop_contract() -> None:
    state = AgentState(user_query="请分析上传的数据")

    assert state.messages == []
    assert state.observations == []
    assert state.iteration == 0
    assert state.data_paths == []


def test_state_normalizes_authorized_data_paths() -> None:
    state = AgentState(
        user_query="生成数据画像",
        data_paths=[" data.csv ", "data.csv", "other.xlsx"],
    )

    assert state.data_paths == [
        str(Path("data.csv").resolve()),
        str(Path("other.xlsx").resolve()),
    ]


def test_record_observation_advances_iteration() -> None:
    state = AgentState(user_query="生成数据画像")
    observation = Observation(
        tool_name=" profile_data ",
        success=True,
        result={"rows": 10, "columns": ["地区", "销量"]},
        duration_ms=12,
    )

    state.record_observation(observation)

    assert state.iteration == 1
    assert state.observations == [observation]
    assert state.observations[0].tool_name == "profile_data"


def test_append_message_validates_content() -> None:
    state = AgentState(user_query="继续分析")

    state.append_message("user", "上一轮问题")
    state.append_message("assistant", "上一轮回答")

    assert state.messages == [
        AgentMessage(role="user", content="上一轮问题"),
        AgentMessage(role="assistant", content="上一轮回答"),
    ]
    with pytest.raises(ValidationError, match="消息内容不能为空"):
        state.append_message("user", "   ")


@pytest.mark.parametrize(
    ("success", "error"),
    [
        (True, "不应存在"),
        (False, None),
        (False, "   "),
    ],
)
def test_observation_rejects_inconsistent_outcome(
    success: bool,
    error: str | None,
) -> None:
    with pytest.raises(ValidationError):
        Observation(tool_name="profile_data", success=success, error=error)


def test_observation_rejects_non_json_result() -> None:
    with pytest.raises(ValidationError):
        Observation(
            tool_name="profile_data",
            success=True,
            result=Path("data.csv"),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"user_query": "   "},
        {"user_query": "画像", "data_paths": [""]},
        {"user_query": "画像", "output_dir": "   "},
        {"user_query": "画像", "iteration": -1},
        {"user_query": "画像", "unknown": True},
    ],
)
def test_state_rejects_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AgentState.model_validate(payload)


def test_state_round_trips_through_json() -> None:
    state = AgentState(
        user_query="生成数据画像",
        data_paths=["data.csv"],
        output_dir="artifacts/run-1",
        messages=[AgentMessage(role="user", content="先看数据质量")],
        observations=[
            Observation(
                tool_name="profile_data",
                success=False,
                error="文件无法读取",
                duration_ms=5,
            )
        ],
        iteration=1,
    )

    restored = AgentState.model_validate_json(state.model_dump_json())

    assert restored == state


def test_agent_loop_receives_structured_observation() -> None:
    state = AgentState(
        user_query="总结画像",
        observations=[
            Observation(
                tool_name="profile_data",
                success=True,
                result={"rows": 10},
            )
        ],
        iteration=1,
    )

    messages = AgentLoop(FakeLlm(), [], "依据观察结果回答").build_messages(state)

    assert '"tool_name": "profile_data"' in messages[-1][1]
    assert '"success": true' in messages[-1][1]
    assert '"rows": 10' in messages[-1][1]


def test_discovers_existing_artifacts_and_excludes_runtime_files(tmp_path) -> None:
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    report = output_dir / "reports" / "summary.md"
    report.parent.mkdir()
    report.write_text("# 已有报告", encoding="utf-8")
    runtime_file = output_dir / ".runtime" / "cache.json"
    runtime_file.parent.mkdir()
    runtime_file.write_text("{}", encoding="utf-8")

    artifacts = discover_artifacts(output_dir)

    assert len(artifacts) == 1
    assert artifacts[0].relative_path == "reports/summary.md"
    assert artifacts[0].absolute_path == str(report.resolve())
    assert artifacts[0].source_tool == "existing_session"
    assert artifacts[0].reusable is True


def test_successful_tool_observation_adds_real_artifact_to_state(tmp_path) -> None:
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    report = output_dir / "report.md"
    report.write_text("真实结果", encoding="utf-8")
    state = AgentState(user_query="生成报告", output_dir=str(output_dir))

    state.record_observation(Observation(
        tool_name="execute_python",
        success=True,
        result={
            "changed_artifacts": ["report.md"],
            "artifact_details": [{
                "path": "report.md",
                "media_type": "text/markdown",
                "size_bytes": report.stat().st_size,
            }],
        },
    ))

    assert len(state.available_artifacts) == 1
    assert state.available_artifacts[0].relative_path == "report.md"
    assert state.available_artifacts[0].absolute_path == str(report.resolve())
    assert state.available_artifacts[0].source_tool == "execute_python"


def test_loop_context_contains_absolute_input_path_and_reusable_artifact(
    tmp_path,
) -> None:
    input_file = tmp_path / "input" / "data.csv"
    input_file.parent.mkdir()
    input_file.write_text("value\n1\n", encoding="utf-8")
    artifact_file = tmp_path / "outputs" / "summary.md"
    artifact_file.parent.mkdir()
    artifact_file.write_text("完成", encoding="utf-8")
    state = AgentState(
        user_query="继续分析",
        data_paths=[str(input_file)],
        available_artifacts=[
            ArtifactReference(
                relative_path="summary.md",
                absolute_path=str(artifact_file),
                name="summary.md",
                size_bytes=artifact_file.stat().st_size,
                media_type="text/markdown",
                source_tool="existing_session",
            )
        ],
    )

    context_text = AgentLoop(FakeLlm(), [], "依据状态回答").build_messages(state)[-1][1]

    assert json.dumps(str(input_file.resolve()), ensure_ascii=False) in context_text
    assert '"artifact_reuse_status": "已有产物可直接复用"' in context_text
    assert '"relative_path": "summary.md"' in context_text


def test_record_observation_keeps_failure_evidence_in_trajectory() -> None:
    """失败的调用不能记为已读文件，但必须留下失败要点。"""
    state = AgentState(user_query="运行代码")

    state.record_observation(Observation(
        tool_name="execute_python",
        success=False,
        error="SyntaxError: invalid syntax",
    ))

    assert state.work_trajectory.failures == [
        "execute_python: SyntaxError: invalid syntax"
    ]
    assert state.work_trajectory.files_read == []
    assert state.work_trajectory.notes == []


def test_absorb_observation_updates_trajectory_without_counting() -> None:
    """运行级聚合只更新轨迹与产物，不保留结果、不推进决策轮次。"""
    state = AgentState(user_query="生成数据画像")

    state.absorb_observation(
        Observation(
            tool_name="profile_data",
            success=True,
            result={"summary": {"rows": 10}},
        )
    )

    assert state.observations == []
    assert state.iteration == 0
    assert state.run_budget.iterations == 0
    assert state.work_trajectory.notes == ["profile_data: rows=10"]


def test_run_budget_is_shared_between_todo_states() -> None:
    """同一运行的多个 Todo 状态必须共享一个决策预算对象。"""
    shared = RunBudget()
    first = AgentState(user_query="第一个 Todo", run_budget=shared)
    second = AgentState(user_query="第二个 Todo", run_budget=shared)
    observation = Observation(tool_name="noop", success=True, result={})

    first.record_observation(observation)
    second.record_observation(observation)

    assert first.run_budget is second.run_budget
    assert shared.iterations == 2
    assert (first.iteration, second.iteration) == (1, 1)


def test_work_trajectory_caps_unbounded_fields() -> None:
    """路径与关键决定必须按上限保留最近条目。"""
    trajectory = WorkTrajectory()

    for index in range(MAX_TRAJECTORY_PATHS + 20):
        trajectory.add_read(f"data/file_{index}.csv")
    for index in range(MAX_TRAJECTORY_DECISIONS + 20):
        trajectory.add_decision(f"决定 {index}")

    assert len(trajectory.files_read) == MAX_TRAJECTORY_PATHS
    assert trajectory.files_read[-1].endswith(f"file_{MAX_TRAJECTORY_PATHS + 19}.csv")
    assert len(trajectory.key_decisions) == MAX_TRAJECTORY_DECISIONS
    assert trajectory.key_decisions[-1] == f"决定 {MAX_TRAJECTORY_DECISIONS + 19}"


def test_work_trajectory_prompt_view_clips_and_keeps_recent() -> None:
    """给 LLM 的轨迹视图只保留最近条目并裁剪单条长度。"""
    trajectory = WorkTrajectory()
    trajectory.add_note("要点" + "长" * 800)
    trajectory.add_failure("execute_python a.py: boom")
    for index in range(30):
        trajectory.add_decision(f"决定 {index}")

    view = trajectory.prompt_view(
        max_paths=1,
        max_decisions=2,
        max_notes=1,
        max_failures=1,
        max_item_chars=50,
    )

    assert view["failures"] == ["execute_python a.py: boom"]
    assert len(view["notes"]) == 1
    assert len(view["notes"][0]) == 50
    assert view["key_decisions"] == ["决定 28", "决定 29"]
    assert view["files_read"] == []


def test_clip_text_marks_truncation() -> None:
    """裁剪必须带统一标记，且上限非法时报错。"""
    assert clip_text("短文本", 10) == "短文本"

    clipped = clip_text("甲" * 50, 20)

    assert len(clipped) == 20
    assert clipped.endswith("（已截断）")
    with pytest.raises(ValueError, match="必须大于 0"):
        clip_text("x", 0)
