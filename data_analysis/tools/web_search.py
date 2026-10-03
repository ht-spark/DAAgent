"""为数据分析 Agent 适配 Tavily 联网搜索能力。"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from pydantic import JsonValue

from .errors import ToolArgumentsError

__all__ = ["MAX_WEB_SEARCH_QUERY_CHARS", "search_web"]

MAX_WEB_SEARCH_QUERY_CHARS = 5_000
LOGGER = logging.getLogger(__name__)


@dataclass
class TavilySearchTool:
    """惰性创建 Tavily 客户端并执行通用网络检索。"""

    api_key: str | None = None
    max_results: int = 5
    search_depth: str = "advanced"
    _client: Any = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "TavilySearchTool":
        """从兼容的环境变量读取 Tavily 密钥。"""
        return cls(
            api_key=(
                os.getenv("TAVILY_API_KEY")
                or os.getenv("Tavily_API_KEY")
                or os.getenv("tavily_api_key")
            )
        )

    def search(self, query: str) -> dict[str, Any]:
        """执行搜索；缺少密钥、依赖或网络失败时返回可诊断的空结果。"""
        if not self.api_key:
            return {"results": [], "answer": "", "error": "API key not configured"}
        try:
            if self._client is None:
                from tavily import TavilyClient

                self._client = TavilyClient(api_key=self.api_key)
            return self._client.search(
                query=query,
                max_results=self.max_results,
                search_depth=self.search_depth,
                include_answer=True,
                include_raw_content=False,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            LOGGER.warning("Tavily 搜索不可用: %s", exc)
            return {"results": [], "answer": "", "error": str(exc)}


def create_search_tool() -> TavilySearchTool:
    """使用环境变量创建 Tavily 搜索工具。"""
    return TavilySearchTool.from_env()


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
