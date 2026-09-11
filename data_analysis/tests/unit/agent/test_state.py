from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from data_analysis.agent.loop import AgentLoop
from data_analysis.agent.state import AgentMessage, AgentState, Observation


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

    assert state.data_paths == ["data.csv", "other.xlsx"]


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
