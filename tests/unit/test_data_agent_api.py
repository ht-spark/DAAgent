"""数据 Agent HTTP 入口的输入边界测试。"""
from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile

from data_analysis.agent.loop import Action
from data_analysis.agent.state import Observation
from server import main


def _upload(name: str, content: bytes = b"x,y\n1,2\n") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


def test_agent_uploads_are_saved_under_isolated_run_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)

    paths = asyncio.run(main._prepare_agent_data_files(
        [_upload("../sales.csv"), _upload("sales.csv")],
        "run-1",
    ))

    assert [path.name for path in paths] == ["sales.csv", "sales-2.csv"]
    assert all(path.parent == (tmp_path / "agent_runs" / "run-1" / "input").resolve() for path in paths)
    assert all(path.read_bytes() == b"x,y\n1,2\n" for path in paths)


def test_agent_upload_rejects_unsupported_file(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)

    with pytest.raises(HTTPException, match="不支持") as exc:
        asyncio.run(main._prepare_agent_data_files([_upload("notes.txt")], "run-2"))

    assert exc.value.status_code == 400
    assert not (tmp_path / "agent_runs" / "run-2").exists()


def test_agent_events_expose_progress_without_tool_arguments():
    action = Action(
        thought="读取已授权数据",
        tool_name="profile_data",
        arguments={"private": "value"},
    )
    observation = Observation(
        tool_name="profile_data",
        success=True,
        result={"summary": {"row_count": 2}},
        duration_ms=8,
    )

    action_payload = main._agent_event_payload(action)
    observation_payload = main._agent_event_payload(observation)

    assert "arguments" not in action_payload
    assert action_payload["thought"] == "读取已授权数据"
    assert observation_payload["summary"] == {"row_count": 2}
