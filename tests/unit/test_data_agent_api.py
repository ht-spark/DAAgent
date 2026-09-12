"""数据 Agent HTTP 入口的输入边界测试。"""
from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile

from data_analysis.agent.loop import Action, Finish
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


def test_agent_finish_exposes_public_reasoning_summary():
    finish = Finish(thought="数据画像已经足以支撑结论", answer="分析完成")

    payload = main._agent_event_payload(finish)

    assert payload == {
        "type": "agent_reasoning",
        "summary": "数据画像已经足以支撑结论",
    }


def test_agent_artifacts_only_include_generated_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)
    run_id = "a" * 32
    run_dir = tmp_path / "agent_runs" / run_id
    (run_dir / "input").mkdir(parents=True)
    (run_dir / "reports").mkdir()
    (run_dir / "input" / "source.csv").write_text("x\n1\n", encoding="utf-8")
    (run_dir / "reports" / "summary.md").write_text("# 结果", encoding="utf-8")

    artifacts = main._list_agent_artifacts(run_id)

    assert artifacts == [
        {
            "run_id": run_id,
            "path": "reports/summary.md",
            "name": "summary.md",
            "size_bytes": 8,
        }
    ]


def test_agent_artifact_resolver_rejects_inputs_and_path_traversal(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)
    run_id = "b" * 32
    input_dir = tmp_path / "agent_runs" / run_id / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "source.csv").write_text("x\n1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="路径无效"):
        main._resolve_agent_artifact(run_id, "input/source.csv")
    with pytest.raises(ValueError, match="路径无效"):
        main._resolve_agent_artifact(run_id, "../outside.txt")
