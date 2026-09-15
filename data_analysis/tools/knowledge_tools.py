"""将现有知识库检索能力封装为 Agent 工具。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from KnowledgeBase.main import retrieve as retrieve_knowledge

from .errors import ToolArgumentsError

MAX_KNOWLEDGE_QUERY_CHARS = 4_000


def search_knowledge_base(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """在用户预选的知识库中检索相关片段。

    Args:
        arguments: Agent 提供的检索问题，只允许包含 ``query``。
        context: 服务端绑定的知识库 ID，不向 Agent 暴露修改入口。

    Returns:
        可供 Agent 继续判断的检索片段及来源信息。

    Raises:
        ToolArgumentsError: 参数或服务端授权上下文不符合约定。
    """
    if set(arguments) != {"query"}:
        raise ToolArgumentsError("search_knowledge_base 只接受必填参数 query")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolArgumentsError("query 必须是非空字符串")
    normalized_query = query.strip()
    if len(normalized_query) > MAX_KNOWLEDGE_QUERY_CHARS:
        raise ToolArgumentsError(
            f"query 不能超过 {MAX_KNOWLEDGE_QUERY_CHARS} 个字符"
        )

    raw_base_ids = context.get("knowledge_base_ids")
    if not isinstance(raw_base_ids, list) or not raw_base_ids or not all(
        isinstance(base_id, str) and base_id.strip() for base_id in raw_base_ids
    ):
        raise ToolArgumentsError("search_knowledge_base 缺少有效的知识库授权上下文")
    selected_base_ids = list(
        dict.fromkeys(base_id.strip() for base_id in raw_base_ids)
    )

    chunks = retrieve_knowledge(
        normalized_query,
        knowledge_base_ids=selected_base_ids,
    )
    matches: list[dict[str, JsonValue]] = [
        {
            "content": chunk.context or chunk.text,
            "source_file": chunk.source_file or "未命名文档",
            "document_id": chunk.document_id,
            "knowledge_base_id": chunk.knowledge_base_id,
            "relevance_score": chunk.rrf_score,
        }
        for chunk in chunks
    ]
    return {
        "query": normalized_query,
        "summary": {"match_count": len(matches)},
        "matches": matches,
    }
