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


def test_discussion_remembers_selected_model_config():
    """每个会话必须独立保存重新进入时使用的模型配置。"""
    first_id = store.create_session("会话一")
    second_id = store.create_session("会话二", model_config_id="cfg_second")

    assert store.set_session_model_config(first_id, "cfg_first") is True
    assert store.get_session(first_id)["model_config_id"] == "cfg_first"
    assert store.get_session(second_id)["model_config_id"] == "cfg_second"
    assert discussions.get_discussion(first_id)["model_config_id"] == "cfg_first"
    listed = {item["id"]: item for item in discussions.list_discussions()}
    assert listed[second_id]["model_config_id"] == "cfg_second"


def test_saved_discussion_retains_attachment_context():
    attachments = [{"kind": "text", "name": "data.json", "content": '{"x": 1}'}]
    discussion_id = discussions.save_discussion_message(
        None,
        "分析附件",
        "已分析",
        [],
        attachments=attachments,
        attachment_context="字段 x，取值 1",
    )

    message = discussions.get_discussion(discussion_id)["messages"][0]
    assert message["attachments"] == attachments
    assert message["metadata"]["attachment_context"] == "字段 x，取值 1"


def test_saved_agent_discussion_retains_mode_and_trace():
    trace = [{"type": "agent_action", "tool_name": "profile_data"}]

    discussion_id = discussions.save_discussion_message(
        None,
        "生成数据画像",
        "画像已完成",
        [],
        mode="agent",
        assistant_metadata={"agent_run_id": "run-1", "agent_trace": trace},
    )

    messages = discussions.get_discussion(discussion_id)["messages"]
    assert messages[0]["metadata"]["mode"] == "agent"
    assert messages[1]["metadata"]["agent_run_id"] == "run-1"
    assert messages[1]["metadata"]["agent_trace"] == trace


def test_agent_discussion_preserves_no_knowledge_base_selection():
    discussion_id = discussions.save_discussion_message(
        None, "直接分析", "已完成", [], knowledge_base_ids=[], mode="agent"
    )

    messages = discussions.get_discussion(discussion_id)["messages"]
    assert [message["metadata"]["knowledge_base_ids"] for message in messages] == [[], []]
    assert [message["metadata"]["knowledge_base_id"] for message in messages] == [None, None]

def test_compaction_entry_stays_in_store_but_is_hidden_from_chat():
    """滚动摘要应持久化，但不能显示成普通聊天消息。"""
    discussion_id = discussions.save_discussion_message(
        None,
        "第一轮问题",
        "第一轮回答",
        [],
        mode="agent",
    )
    store.append_message(
        discussion_id,
        store.ROLE_TOOL,
        "## Goal" + chr(10) + "保留任务目标",
        metadata={
            "kind": "agent_compaction",
            "covered_through_seq": 2,
            "summary_version": 1,
        },
    )

    stored = store.get_session(discussion_id)
    visible = discussions.get_discussion(discussion_id)

    assert stored is not None
    assert len(stored["messages"]) == 3
    assert visible is not None
    assert [message["role"] for message in visible["messages"]] == [
        "user",
        "assistant",
    ]
