"""数据 Agent HTTP 入口的输入边界测试。"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from data_analysis.agent.loop import Action, Finish
from data_analysis.agent.state import Observation
from server import main
from scr.math_modeling_agent import llm


def _upload(name: str, content: bytes = b"x,y\n1,2\n") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


def test_agent_timeout_configuration_is_separated():
    assert llm.LLM_CALL_TIMEOUT_SECONDS == 180
    assert main.AGENT_LLM_TIMEOUT_SECONDS == 180
    assert main.AGENT_TOOL_TIMEOUT_SECONDS == 2 * 60 * 60
    assert main.AGENT_RUN_TIMEOUT_SECONDS == 3 * 60 * 60


def test_agent_uploads_are_saved_under_session_run_input_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)

    paths = asyncio.run(main._prepare_agent_data_files(
        [_upload("../sales.csv"), _upload("sales.csv")],
        "discussion-1",
        "run-1",
    ))

    assert [path.name for path in paths] == ["sales.csv", "sales-2.csv"]
    assert all(path.parent == (tmp_path / "agent_sessions" / "discussion-1" / ".runs" / "run-1" / "input").resolve() for path in paths)
    assert all(path.read_bytes() == b"x,y\n1,2\n" for path in paths)


def test_agent_upload_rejects_unsupported_file(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)

    with pytest.raises(HTTPException, match="不支持") as exc:
        asyncio.run(main._prepare_agent_data_files([_upload("notes.txt")], "discussion-2", "run-2"))

    assert exc.value.status_code == 400
    assert not (tmp_path / "agent_sessions" / "discussion-2").exists()


@pytest.mark.parametrize(
    "file_name",
    [
        "report.pdf",
        "report.docx",
        "notes.md",
        "notes.markdown",
        "records.json",
        "figure.png",
        "figure.jpg",
        "figure.webp",
        "figure.bmp",
        "figure.tiff",
    ],
)
def test_agent_upload_accepts_documents_data_and_images(
    monkeypatch,
    tmp_path,
    file_name: str,
):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)

    paths = asyncio.run(
        main._prepare_agent_data_files(
            [_upload(file_name, b"file-content")],
            "discussion-files",
            f"run-{Path(file_name).suffix[1:]}",
        )
    )

    assert [path.name for path in paths] == [file_name]
    assert paths[0].read_bytes() == b"file-content"


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


def test_agent_code_event_exposes_generated_code_only():
    action = Action(
        thought="计算描述统计",
        tool_name="execute_python",
        arguments={"code": "result = {'mean': 2.0}"},
    )

    payload = main._agent_event_payload(action)

    assert payload == {
        "type": "agent_action",
        "thought": "计算描述统计",
        "tool_name": "execute_python",
        "code": "result = {'mean': 2.0}",
    }


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
    discussion_id = "discussion-artifacts"
    run_dir = tmp_path / "agent_sessions" / discussion_id
    (run_dir / ".runs" / run_id / "input").mkdir(parents=True)
    (run_dir / "reports").mkdir()
    (run_dir / ".runs" / run_id / "input" / "source.csv").write_text("x\n1\n", encoding="utf-8")
    (run_dir / "reports" / "summary.md").write_text("# 结果", encoding="utf-8")

    artifacts = main._list_agent_artifacts(discussion_id, run_id)

    assert artifacts == [
        {
            "discussion_id": discussion_id,
            "run_id": run_id,
            "path": "reports/summary.md",
            "name": "summary.md",
            "size_bytes": 8,
        }
    ]


def test_agent_artifacts_are_shared_across_runs_in_same_discussion(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)
    discussion_id = "discussion-shared"
    root = tmp_path / "agent_sessions" / discussion_id
    root.mkdir(parents=True)
    (root / "first.md").write_text("第一轮", encoding="utf-8")

    first = main._list_agent_artifacts(discussion_id, "run-1")
    (root / "second.md").write_text("第二轮", encoding="utf-8")
    second = main._list_agent_artifacts(discussion_id, "run-2")

    assert [item["path"] for item in first] == ["first.md"]
    assert [item["path"] for item in second] == ["first.md", "second.md"]
    assert all(item["discussion_id"] == discussion_id for item in second)


def test_agent_text_artifact_preview_is_safe_and_bounded(monkeypatch, tmp_path):
    target = tmp_path / "analysis.py"
    target.write_text("print('preview')", encoding="utf-8")
    monkeypatch.setattr(main, "MAX_AGENT_PREVIEW_TEXT_BYTES", 8)

    response = main._agent_artifact_preview(target)

    assert response.body.decode("utf-8") == "print('p"
    assert response.headers["X-Preview-Truncated"] == "true"
    assert response.media_type == "text/plain"


def test_agent_docx_artifact_preview_extracts_content(tmp_path):
    from docx import Document

    target = tmp_path / "report.docx"
    document = Document()
    document.add_paragraph("分析结论")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "指标"
    table.rows[0].cells[1].text = "数值"
    document.save(target)

    response = main._agent_artifact_preview(target)
    payload = json.loads(response.body)

    assert payload["kind"] == "docx"
    assert payload["paragraphs"] == ["分析结论"]
    assert payload["tables"] == [[["指标", "数值"]]]


def test_agent_xlsx_artifact_preview_extracts_sheets(tmp_path):
    from openpyxl import Workbook

    target = tmp_path / "metrics.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "结果"
    sheet.append(["指标", "数值"])
    sheet.append(["均值", 2.5])
    workbook.save(target)
    workbook.close()

    response = main._agent_artifact_preview(target)
    payload = json.loads(response.body)

    assert payload["kind"] == "xlsx"
    assert payload["sheets"] == [
        {"name": "结果", "rows": [["指标", "数值"], ["均值", 2.5]]}
    ]


def test_agent_artifact_preview_rejects_unsupported_type(tmp_path):
    target = tmp_path / "archive.zip"
    target.write_bytes(b"not-a-preview")

    with pytest.raises(HTTPException) as exc:
        main._agent_artifact_preview(target)

    assert exc.value.status_code == 415


def test_agent_artifacts_are_shared_across_runs_in_same_discussion(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)
    discussion_id = "discussion-shared"
    root = tmp_path / "agent_sessions" / discussion_id
    root.mkdir(parents=True)
    (root / "first.md").write_text("第一轮", encoding="utf-8")

    first = main._list_agent_artifacts(discussion_id, "run-1")
    (root / "second.md").write_text("第二轮", encoding="utf-8")
    second = main._list_agent_artifacts(discussion_id, "run-2")

    assert [item["path"] for item in first] == ["first.md"]
    assert [item["path"] for item in second] == ["first.md", "second.md"]
    assert all(item["discussion_id"] == discussion_id for item in second)


def test_agent_artifact_resolver_rejects_inputs_and_path_traversal(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)
    run_id = "b" * 32
    discussion_id = "discussion-resolver"
    input_dir = tmp_path / "agent_sessions" / discussion_id / ".runs" / run_id / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "source.csv").write_text("x\n1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="路径无效"):
        main._resolve_agent_artifact(discussion_id, ".runs/b/input/source.csv")
    with pytest.raises(ValueError, match="路径无效"):
        main._resolve_agent_artifact(discussion_id, "../outside.txt")


def test_open_agent_artifact_folder_uses_validated_session_directory(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)
    target = tmp_path / "agent_sessions" / "discussion-open"
    target.mkdir(parents=True)
    opened: list[Path] = []
    monkeypatch.setattr(main, "_open_local_folder", opened.append)

    result = main.open_agent_artifact_folder("discussion-open")

    assert result == {"opened": True}
    assert opened == [target.resolve()]


def test_open_agent_artifact_folder_rejects_missing_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)

    with pytest.raises(HTTPException) as exc:
        main.open_agent_artifact_folder("discussion-missing")

    assert exc.value.status_code == 404


def test_deleting_discussion_removes_its_artifact_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)
    monkeypatch.setattr(main, "delete_discussion", lambda _discussion_id: True)
    root = tmp_path / "agent_sessions" / "discussion-delete"
    root.mkdir(parents=True)
    (root / "result.md").write_text("结果", encoding="utf-8")

    result = asyncio.run(
        main.delete_brainstorm_discussion_endpoint("discussion-delete")
    )

    assert result == {"deleted": True}
    assert not root.exists()


def test_agent_restores_existing_session_attachments(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path / "artifacts")
    old_input = (
        tmp_path
        / "artifacts"
        / "agent_sessions"
        / "discussion-restore"
        / ".runs"
        / "old-run"
        / "input"
    )
    old_input.mkdir(parents=True)
    source = old_input / "sales.csv"
    source.write_text("x,y\n1,2\n", encoding="utf-8")
    entries = [
        {
            "role": "user",
            "metadata": {
                "attachments": [
                    {
                        "kind": "data",
                        "name": "sales.csv",
                        "content": (
                            "artifacts/agent_sessions/discussion-restore/"
                            ".runs/old-run/input/sales.csv"
                        ),
                    }
                ]
            },
        }
    ]

    restored = main._restore_agent_data_files(entries, "discussion-restore")

    assert restored == [source.resolve()]


def test_agent_ignores_missing_and_out_of_session_attachments(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path / "artifacts")
    outside = tmp_path / "outside.csv"
    outside.write_text("x\n1\n", encoding="utf-8")
    entries = [
        {
            "role": "user",
            "metadata": {
                "attachments": [
                    {"kind": "data", "name": "outside.csv", "content": "outside.csv"},
                    {
                        "kind": "data",
                        "name": "missing.csv",
                        "content": (
                            "artifacts/agent_sessions/discussion-safe/"
                            ".runs/old/input/missing.csv"
                        ),
                    },
                    {"kind": "text", "name": "inline.txt", "content": "not-a-path"},
                ]
            },
        }
    ]

    restored = main._restore_agent_data_files(entries, "discussion-safe")

    assert restored == []


def test_agent_new_upload_replaces_same_named_restored_attachment(tmp_path):
    restored = tmp_path / "old" / "sales.csv"
    uploaded = tmp_path / "new" / "sales.csv"
    other = tmp_path / "old" / "weather.csv"

    merged = main._merge_agent_data_files([restored, other], [uploaded])

    assert merged == [other, uploaded]


def test_agent_restored_attachments_respect_active_file_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MAX_AGENT_ATTACHMENTS", 2)
    restored = [tmp_path / f"old-{index}.csv" for index in range(3)]

    merged = main._merge_agent_data_files(restored, [])

    assert merged == restored[-2:]
