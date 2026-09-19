"""为数据分析 Agent 适配 Tavily 联网搜索能力。"""

from __future__ import annotations

from collections.abc import Mapping

from dotenv import load_dotenv
from pydantic import JsonValue

from scr.tools.tavily_search import TavilySearchTool, create_search_tool

from .errors import ToolArgumentsError

__all__ = ["MAX_WEB_SEARCH_QUERY_CHARS", "search_web"]

MAX_WEB_SEARCH_QUERY_CHARS = 5_000


def search_web(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """使用项目已有的 Tavily 工具检索互联网。

    Args:
        arguments: 仅包含搜索问题 ``query`` 的工具参数。
        context: 可选的服务端 Tavily 密钥上下文，不会暴露给模型。

    Returns:
        Tavily 返回的摘要、来源链接与相关结果。

    Raises:
        ToolArgumentsError: 调用参数不符合工具约定。
    """
    if set(arguments) != {"query"}:
        raise ToolArgumentsError("search_web 只接受必填参数 query")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolArgumentsError("query 必须是非空字符串")
    normalized_query = query.strip()
    if len(normalized_query) > MAX_WEB_SEARCH_QUERY_CHARS:
        raise ToolArgumentsError(
            f"query 不能超过 {MAX_WEB_SEARCH_QUERY_CHARS} 个字符"
        )

    load_dotenv()
    api_key = context.get("api_key")
    search_tool = (
        TavilySearchTool(api_key=api_key.strip())
        if isinstance(api_key, str) and api_key.strip()
        else create_search_tool()
    )
    return search_tool.search(normalized_query)
