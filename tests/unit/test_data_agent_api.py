"""数据 Agent HTTP 入口的输入边界测试。"""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from data_analysis.agent.loop import Action, Finish
from data_analysis.agent.state import Observation
from server import main
from server.schemas import AgentCommandDecisionBody
from data_analysis import llm


def _upload(name: str, content: bytes = b"x,y\n1,2\n") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


def test_agent_timeout_configuration_is_separated():
    assert llm.LLM_CALL_TIMEOUT_SECONDS == 180
    assert main.AGENT_LLM_TIMEOUT_SECONDS == 180
    assert main.AGENT_TOOL_TIMEOUT_SECONDS == 2 * 60 * 60
    assert main.AGENT_RUN_TIMEOUT_SECONDS == 3 * 60 * 60


@pytest.mark.asyncio
async def test_agent_chat_run_replays_events_after_subscriber_disconnect():
    """客户端不订阅时后台运行仍应结束，重连后可回放完整轨迹。"""
    run = main.AgentChatRun("discussion-replay", "分析数据")
    continue_run = asyncio.Event()

    async def events():
        yield '{"type":"agent_started"}\n'
        await continue_run.wait()
        yield '{"type":"done"}\n'

    producer = asyncio.create_task(main._publish_agent_chat_run(run, events()))
    subscriber = main._replay_agent_chat_run(run)
    assert await anext(subscriber) == '{"type":"agent_started"}\n'
    await subscriber.aclose()
    continue_run.set()
    await producer
    replayed = [line async for line in main._replay_agent_chat_run(run)]

    assert replayed == ['{"type":"agent_started"}\n', '{"type":"done"}\n']
    assert run.done


@pytest.mark.asyncio
async def test_agent_chat_run_cleans_temporary_attachments(monkeypatch, tmp_path):
    """后台运行结束后应删除本轮上传原文件。"""
    runtime_root = tmp_path / "agent_uploads"
    run_dir = runtime_root / "run-cleanup"
    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "notes.txt").write_text("临时附件", encoding="utf-8")
    monkeypatch.setattr(main, "AGENT_RUNTIME_ROOT", runtime_root)
    run = main.AgentChatRun(
        "discussion-cleanup",
        "分析附件",
        runtime_dir=run_dir,
    )

    async def events():
        yield '{"type":"done"}\n'

    await main._publish_agent_chat_run(run, events())

    assert run.done
    assert not run_dir.exists()


@pytest.mark.asyncio
async def test_cancel_agent_chat_run_rejects_pending_command_approval():
    """终止请求应设置标记并立即拒绝当前等待审批的命令。"""
    run = main.AgentChatRun("discussion-cancel", "运行命令")
    run.approval_id = "approval-cancel"
    decision = asyncio.get_running_loop().create_future()
    main._agent_chat_runs["run-cancel"] = run
    main._pending_agent_commands["approval-cancel"] = decision
    try:
        result = await main.cancel_agent_chat_run("run-cancel")
        assert result == {"accepted": True}
        assert run.cancel_requested
        assert await decision is False
    finally:
        main._agent_chat_runs.pop("run-cancel", None)
        main._pending_agent_commands.pop("approval-cancel", None)


@pytest.mark.asyncio
async def test_cancel_one_agent_run_keeps_other_run_active():
    """终止指定任务不应影响另一个会话的后台运行。"""
    first = main.AgentChatRun("discussion-first", "任务一")
    second = main.AgentChatRun("discussion-second", "任务二")
    main._agent_chat_runs["run-first"] = first
    main._agent_chat_runs["run-second"] = second
    try:
        assert await main.cancel_agent_chat_run("run-first") == {"accepted": True}
        assert first.cancel_requested
        assert not second.cancel_requested
        assert await main.get_active_agent_chat_run("discussion-second") == {
            "run_id": "run-second",
            "message": "任务二",
        }
    finally:
        main._agent_chat_runs.pop("run-first", None)
        main._agent_chat_runs.pop("run-second", None)


@pytest.mark.asyncio
async def test_deleting_running_discussion_is_rejected(monkeypatch, tmp_path):
    """运行中的会话不能删除，以免后台任务继续写入已删除目录。"""
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)
    run = main.AgentChatRun("discussion-running", "分析数据")
    main._agent_chat_runs["run-running"] = run
    try:
        with pytest.raises(HTTPException) as exc:
            await main.delete_brainstorm_discussion_endpoint("discussion-running")
        assert exc.value.status_code == 409
    finally:
        main._agent_chat_runs.pop("run-running", None)

@pytest.mark.asyncio
async def test_agent_command_decision_requires_pending_request():
    with pytest.raises(HTTPException) as exc:
        await main.decide_agent_command_endpoint(
            "missing", AgentCommandDecisionBody(approved=True),
        )
    assert exc.value.status_code == 409

    decision = asyncio.get_running_loop().create_future()
    main._pending_agent_commands["test-request"] = decision
    try:
        result = await main.decide_agent_command_endpoint(
            "test-request", AgentCommandDecisionBody(approved=False),
        )
        assert result == {"accepted": True}
        assert await decision is False
        with pytest.raises(HTTPException):
            await main.decide_agent_command_endpoint(
                "test-request", AgentCommandDecisionBody(approved=True),
            )
    finally:
        main._pending_agent_commands.pop("test-request", None)


def test_agent_uploads_are_saved_under_temporary_run_directory(monkeypatch, tmp_path):
    runtime_root = tmp_path / "agent_uploads"
    monkeypatch.setattr(main, "AGENT_RUNTIME_ROOT", runtime_root)

    paths = asyncio.run(main._prepare_agent_data_files(
        [_upload("../sales.csv"), _upload("sales.csv")],
        "discussion-1",
        "run-1",
    ))

    assert [path.name for path in paths] == ["sales.csv", "sales-2.csv"]
    assert all(path.parent == (runtime_root / "run-1" / "input").resolve() for path in paths)
    assert all(path.read_bytes() == b"x,y\n1,2\n" for path in paths)
    assert not (tmp_path / "artifacts").exists()

    main._cleanup_agent_runtime_dir(paths[0].parents[1])

    assert not (runtime_root / "run-1").exists()


def test_agent_upload_rejects_unsupported_file(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "AGENT_RUNTIME_ROOT", tmp_path / "agent_uploads")

    with pytest.raises(HTTPException, match="不支持") as exc:
        asyncio.run(main._prepare_agent_data_files([_upload("program.exe")], "discussion-2", "run-2"))

    assert exc.value.status_code == 400
    assert not (tmp_path / "agent_uploads" / "run-2").exists()


def test_agent_upload_size_error_uses_configured_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "AGENT_RUNTIME_ROOT", tmp_path / "agent_uploads")
    monkeypatch.setattr(main, "MAX_AGENT_ATTACHMENT_BYTES", 2 * 1024 * 1024)

    with pytest.raises(HTTPException, match="2 MB 限制"):
        asyncio.run(main._prepare_agent_data_files(
            [_upload("large.txt", b"x" * (2 * 1024 * 1024 + 1))],
            "discussion-size",
            "run-size",
        ))


def test_startup_cleanup_removes_only_stale_runtime_directories(
    monkeypatch,
    tmp_path,
):
    runtime_root = tmp_path / "agent_uploads"
    stale = runtime_root / "stale-run"
    fresh = runtime_root / "fresh-run"
    stale.mkdir(parents=True)
    fresh.mkdir()
    now = 2_000_000_000.0
    os.utime(stale, (now - main.AGENT_RUNTIME_MAX_AGE_SECONDS - 1,) * 2)
    os.utime(fresh, (now, now))
    monkeypatch.setattr(main, "AGENT_RUNTIME_ROOT", runtime_root)

    removed = main._cleanup_stale_agent_runtime_dirs(now=now)

    assert removed == [stale.resolve()]
    assert not stale.exists()
    assert fresh.exists()


@pytest.mark.parametrize(
    "file_name",
    [
        "report.pdf",
        "report.docx",
        "notes.md",
        "notes.markdown",
        "notes.txt",
        "analysis.py",
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
    monkeypatch.setattr(main, "AGENT_RUNTIME_ROOT", tmp_path / "agent_uploads")

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


def test_knowledge_search_observation_includes_source_citations():
    observation = Observation(
        tool_name="search_knowledge_base",
        success=True,
        result={
            "summary": {"match_count": 2},
            "matches": [
                {
                    "content": "扩散模型用于时间序列预测的核心机制……",
                    "source_file": "DIFFUSION-BASED DECOUPLED.md",
                    "document_id": "doc-1",
                    "knowledge_base_id": "kb-1",
                    "relevance_score": 0.9,
                },
                {
                    "content": "另一段命中内容",
                    "source_file": "RAG笔记.md",
                    "document_id": "doc-2",
                    "knowledge_base_id": "kb-1",
                },
            ],
        },
        duration_ms=12,
    )

    payload = main._agent_event_payload(observation)

    sources = payload["sources"]
    assert [item["source_file"] for item in sources] == [
        "DIFFUSION-BASED DECOUPLED.md",
        "RAG笔记.md",
    ]
    assert sources[0]["document_id"] == "doc-1"
    assert sources[0]["excerpt"].startswith("扩散模型用于时间序列预测")


def test_merge_knowledge_sources_dedupes_by_document_and_file():
    first = {
        "source_file": "a.md",
        "document_id": "doc-a",
        "knowledge_base_id": "kb",
        "excerpt": "一",
    }
    second = {
        "source_file": "b.md",
        "document_id": "doc-b",
        "knowledge_base_id": "kb",
        "excerpt": "二",
    }

    merged = main._merge_knowledge_sources(
        [first],
        [first, second],
    )

    assert merged == [first, second]


def test_project_edit_and_command_results_are_visible_in_trace():
    edit = Observation(
        tool_name="edit_workspace_file", success=True,
        result={"path": "src/a.py", "diff": "-old\n+new\n"},
    )
    command = Observation(
        tool_name="run_terminal_command", success=False,
        result={"command": "python -m pytest", "exit_code": 1,
                "shell": "powershell", "output": "FAILED test_a",
                "changed_files": ["coverage.xml"],
                "changes_truncated": False},
        error="命令退出码 1: FAILED test_a",
    )

    assert "-old" in main._agent_event_payload(edit)["details"]
    assert "exit_code: 1" in main._agent_event_payload(command)["details"]
    assert "FAILED test_a" in main._agent_event_payload(command)["details"]
    assert "coverage.xml" in main._agent_event_payload(command)["details"]

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


def test_agent_action_exposes_summary_arguments_only():
    """轨迹单行摘要只下发短参数白名单（路径/命令/查询词），并截断超长值。"""
    edit = Action(
        thought="修改文件",
        tool_name="edit_workspace_file",
        arguments={
            "relative_path": "src/a.py",
            "old_text": "o" * 500,
            "new_text": "n" * 500,
            "content": "c" * 500,
        },
    )

    assert main._agent_event_payload(edit)["arguments"] == {"relative_path": "src/a.py"}

    search = Action(
        thought="搜索关键词",
        tool_name="search_workspace_text",
        arguments={"query": "相关系数", "pattern": None},
    )

    assert main._agent_event_payload(search)["arguments"] == {"query": "相关系数"}

    command = Action(
        thought="运行测试",
        tool_name="run_terminal_command",
        arguments={"command": "python -m pytest " + "a" * 500},
    )

    command_args = main._agent_event_payload(command)["arguments"]
    assert len(command_args["command"]) == main.SUMMARY_ARGUMENT_MAX_LENGTH


def test_agent_finish_exposes_public_reasoning_summary():
    finish = Finish(thought="数据画像已经足以支撑结论", answer="分析完成")

    payload = main._agent_event_payload(finish)

    assert payload == {
        "type": "agent_reasoning",
        "summary": "数据画像已经足以支撑结论",
    }


def test_final_answer_is_split_into_streaming_chunks_without_content_loss():
    """最终回答应分块推送，且拼接后与模型原文完全一致。"""
    answer = "第一段分析结论。\n\n第二段包含 **Markdown** 与中文内容。" * 4

    chunks = main._final_answer_chunks(answer)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= main.FINAL_ANSWER_CHUNK_CHARS for chunk in chunks)
    assert "".join(chunks) == answer


def test_agent_events_only_expose_public_thought_summary():
    """决策事件只公开 thought 摘要，不透传模型私有推理链。"""
    action = Action(
        thought="读取数据",
        tool_name="profile_data",
        arguments={},
        reasoning="先确认字段类型，再决定画像指标。",
    )
    finish = Finish(
        thought="信息充分",
        answer="分析完成",
        reasoning="已有足够观察，可以作答。",
    )

    action_payload = main._agent_event_payload(action)
    finish_payload = main._agent_event_payload(finish)

    assert action_payload["thought"] == "读取数据"
    assert finish_payload["summary"] == "信息充分"
    assert "model_reasoning" not in action_payload
    assert "model_reasoning" not in finish_payload


def test_agent_artifacts_only_include_generated_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ARTIFACTS_ROOT", tmp_path)
    run_id = "a" * 32
    discussion_id = "discussion-artifacts"
    run_dir = tmp_path / "agent_sessions" / discussion_id
    (run_dir / ".runs" / run_id / "input").mkdir(parents=True)
    (run_dir / "reports").mkdir()
    (run_dir / ".runtime").mkdir()
    (run_dir / ".runs" / run_id / "input" / "source.csv").write_text("x\n1\n", encoding="utf-8")
    (run_dir / ".runtime" / "cache.json").write_text("{}", encoding="utf-8")
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
    runtime_dir = input_dir.parents[2] / ".runtime"
    runtime_dir.mkdir()
    (runtime_dir / "cache.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="路径无效"):
        main._resolve_agent_artifact(discussion_id, ".runs/b/input/source.csv")
    with pytest.raises(ValueError, match="路径无效"):
        main._resolve_agent_artifact(discussion_id, ".runtime/cache.json")
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


def test_agent_compiles_text_attachment_into_context(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("指标口径：含税销售额", encoding="utf-8")

    context = main._compile_uploaded_attachment_context([source], object())

    assert "notes.txt" in context
    assert "指标口径：含税销售额" in context


def test_agent_structured_data_attachment_skips_llm_summary(tmp_path):
    """大型结构化数据应交给数据工具读取，启动阶段不得调用模型做全文摘要。"""
    source = tmp_path / "orders.csv"
    source.write_text(
        "order_id,amount\n"
        + "\n".join(f"{index},{index * 2}" for index in range(20_000)),
        encoding="utf-8",
    )

    class RejectingLlm:
        """一旦附件编译错误调用模型，就立即暴露回归。"""

        def invoke(self, _messages):
            raise AssertionError("结构化数据附件不应在启动阶段调用模型")

    context = main._compile_uploaded_attachment_context([source], RejectingLlm())

    assert "结构化数据附件：orders.csv" in context
    assert "profile_data" in context
    assert "inspect_data_rows" in context
    assert "0,0" not in context


def test_attachment_metadata_does_not_persist_temporary_path(tmp_path):
    source = tmp_path / "sales.csv"
    source.write_text("x\n1\n", encoding="utf-8")

    metadata = main._attachment_metadata([source])

    assert metadata == [{
        "kind": "data",
        "name": "sales.csv",
        "content": "已加载到会话上下文",
    }]
    assert str(tmp_path) not in str(metadata)
