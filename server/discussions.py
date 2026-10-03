"""基于统一 store（sessions + messages）的读写。

对外函数签名与旧 JSON 实现保持一致，供 server.main 直接调用。
"""
from __future__ import annotations

from typing import Any

from data_analysis.context.rolling_memory import COMPACTION_KIND

from server import store


def list_discussions(project_id: str | None = None) -> list[dict[str, Any]]:
    """按最近更新返回会话摘要，可按项目筛选。"""
    return [
        {
            "id": item["id"],
            "title": item["title"],
            "project_id": item.get("project_id"),
            "model_config_id": item.get("model_config_id"),
            "updated_at": item["updated_at"],
        }
        for item in store.list_sessions(project_id=project_id)
    ]


def get_discussion(discussion_id: str) -> dict[str, Any] | None:
    """返回完整会话。消息字段兼容旧结构：sources/attachments 在顶层。"""
    session = store.get_session(discussion_id)
    if session is None:
        return None
    messages: list[dict[str, Any]] = []
    for raw in session.get("messages", []):
        meta = raw.get("metadata") or {}
        if meta.get("kind") == COMPACTION_KIND:
            continue
        messages.append(
            {
                "id": raw.get("id"),
                "role": raw.get("role"),
                "content": raw.get("content") or "",
                "sources": meta.get("sources") or [],
                "attachments": meta.get("attachments"),
                "metadata": meta,
            }
        )
    return {
        "id": session["id"],
        "title": session["title"],
        "project_id": session.get("project_id"),
        "cwd": session.get("cwd"),
        "model_config_id": session.get("model_config_id"),
        "created_at": session.get("created_at"),
        "updated_at": session["updated_at"],
        "messages": messages,
    }


def delete_discussion(discussion_id: str) -> bool:
    return store.delete_session(discussion_id)


def rename_discussion(discussion_id: str, title: str) -> bool:
    return store.rename_session(discussion_id, title)


def save_discussion_message(
    discussion_id: str | None,
    user_message: str,
    assistant_message: str,
    sources: list[dict[str, str]],
    title: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    knowledge_base_id: str = "default",
    knowledge_base_ids: list[str] | None = None,
    mode: str = "inspiration",
    assistant_metadata: dict[str, Any] | None = None,
    project_id: str | None = None,
    cwd: str | None = None,
    attachment_context: str | None = None,
) -> str:
    """追加一轮 user/assistant 消息，返回会话 id。"""
    normalized_title = (title or user_message or "新讨论").strip()[:80]
    session_id = discussion_id or store.create_session(
        normalized_title,
        project_id=project_id,
        cwd=cwd,
    )
    existing = store.get_session(session_id)
    if existing is None:
        store.create_session(
            normalized_title,
            session_id=session_id,
            project_id=project_id,
            cwd=cwd,
        )
    elif project_id is not None and cwd is not None:
        if not store.bind_session_project(session_id, project_id, cwd):
            raise ValueError("会话已经绑定到其他项目")
    if title and title.strip():
        store.rename_session(session_id, title)

    selected_base_ids = knowledge_base_ids if knowledge_base_ids is not None else [knowledge_base_id]
    user_meta: dict[str, Any] = {
        "sources": [],
        "knowledge_base_id": selected_base_ids[0] if selected_base_ids else None,
        "knowledge_base_ids": selected_base_ids,
        "mode": mode,
    }
    if attachments:
        user_meta["attachments"] = attachments
    if isinstance(attachment_context, str) and attachment_context.strip():
        user_meta["attachment_context"] = attachment_context.strip()
    store.append_message(
        session_id,
        store.ROLE_USER,
        user_message,
        metadata=user_meta,
    )
    response_meta: dict[str, Any] = {
        "sources": sources or [],
        "knowledge_base_id": selected_base_ids[0] if selected_base_ids else None,
        "knowledge_base_ids": selected_base_ids,
        "mode": mode,
    }
    if assistant_metadata:
        response_meta.update(assistant_metadata)
    store.append_message(
        session_id,
        store.ROLE_ASSISTANT,
        assistant_message,
        metadata=response_meta,
    )
    return session_id
