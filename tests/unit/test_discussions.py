"""Tests for local inspiration discussion persistence (unified store)."""
from __future__ import annotations

import pytest

from server import discussions, store


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"
    monkeypatch.setattr(store, "_DB_PATH", db_path)
    monkeypatch.setattr(store, "_conn", None)
    store._connect()
    yield
    if store._conn is not None:
        store._conn.close()
        monkeypatch.setattr(store, "_conn", None)


def test_saved_discussion_can_be_listed_and_restored():
    discussion_id = discussions.save_discussion_message(
        None,
        "如何建立预测模型？",
        "可以先明确预测目标。",
        [{"source_file": "reference.md", "document_id": "doc-id", "content": "预测资料"}],
    )

    assert discussions.list_discussions()[0]["id"] == discussion_id
    restored = discussions.get_discussion(discussion_id)
    assert restored is not None
    assert restored["messages"][1]["sources"][0]["source_file"] == "reference.md"


def test_delete_discussion_removes_persisted_messages():
    discussion_id = discussions.save_discussion_message(None, "问题", "回答", [])

    assert discussions.delete_discussion(discussion_id) is True
    assert discussions.get_discussion(discussion_id) is None
    assert discussions.list_discussions() == []


def test_discussion_title_is_user_supplied_and_can_be_renamed():
    discussion_id = discussions.save_discussion_message(
        None, "问题", "回答", [], "预测模型讨论"
    )

    assert discussions.list_discussions()[0]["title"] == "预测模型讨论"
    assert discussions.rename_discussion(discussion_id, "最终方案") is True
    assert discussions.list_discussions()[0]["title"] == "最终方案"


def test_saved_discussion_retains_attachment_context():
    attachments = [{"kind": "text", "name": "data.json", "content": '{"x": 1}'}]
    discussion_id = discussions.save_discussion_message(
        None, "分析附件", "已分析", [], attachments=attachments
    )

    assert discussions.get_discussion(discussion_id)["messages"][0]["attachments"] == attachments


def test_modeling_tool_message_lives_in_same_store():
    """建模 run 与聊天共用同一会话库。"""
    from server import runs

    session_id = store.create_session("建模会话")
    runs.create_run(
        "runx01",
        "预测某指标",
        {"provider": "openai", "api_key": "secret"},
        task_name="预测任务",
        session_id=session_id,
    )
    runs.append_progress("runx01", {"type": "node", "node": "intake"})

    listed = runs.list_runs()
    assert any(item["run_id"] == "runx01" for item in listed)
    detail = runs.get_run("runx01")
    assert detail is not None
    assert detail["status"] == "queued"
    assert detail["progress"][0]["node"] == "intake"
    assert "secret" not in str(detail["model_config"])

    session = store.get_session(session_id)
    assert any(m["id"] == "runx01" and m["role"] == "tool" for m in session["messages"])
