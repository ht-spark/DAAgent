"""Tavily 联网搜索工具。

通用的联网搜索接口：按调用方构造的查询词检索网络资料，
返回原始搜索结果。工具层不预设题型分类或方法目录，
查询词如何构造由上层（如方法探索阶段的 LLM 流程）决定。

设计要点：
  - API key 从环境变量读取（Tavily_API_KEY / TAVILY_API_KEY）
  - 无 API key 或网络错误时优雅降级（返回空结果，不中断流程）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "TavilySearchTool",
    "create_search_tool",
]


@dataclass
class TavilySearchTool:
    """Tavily 联网搜索工具。

    Attributes:
        api_key: Tavily API 密钥。若为 None 则搜索功能不可用。
        client: TavilyClient 实例（惰性初始化）。
        max_results: 每次搜索返回的最大结果数。
        search_depth: 搜索深度（"basic" 或 "advanced"）。
        timeout: 请求超时秒数。
    """

    api_key: str | None = None
    max_results: int = 5
    search_depth: str = "advanced"
    timeout: int = 30
    _client: Any = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, **kwargs: Any) -> "TavilySearchTool":
        """从环境变量创建搜索工具。

        读取以下环境变量（按优先级）：
          1. TAVILY_API_KEY
          2. Tavily_API_KEY
          3. tavily_api_key

        若均不存在，返回 api_key=None 的实例（搜索功能降级为空）。
        """
        api_key = (
            os.getenv("TAVILY_API_KEY")
            or os.getenv("Tavily_API_KEY")
            or os.getenv("tavily_api_key")
        )
        return cls(api_key=api_key, **kwargs)

    # ------------------------------------------------------------------
    # 客户端管理
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """搜索工具是否可用（有 API key）。"""
        return self.api_key is not None and len(self.api_key) > 0

    def _get_client(self) -> Any:
        """惰性初始化 TavilyClient。"""
        if self._client is not None:
            return self._client
        if not self.available:
            return None
        try:
            from tavily import TavilyClient

            self._client = TavilyClient(api_key=self.api_key)
            return self._client
        except ImportError:
            print("[tavily] 警告：tavily-python 未安装，联网搜索不可用")
            return None
        except Exception as e:
            print(f"[tavily] 警告：TavilyClient 初始化失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 通用搜索
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        max_results: int | None = None,
        search_depth: str | None = None,
        include_answer: bool = True,
        include_raw_content: bool = False,
    ) -> dict[str, Any]:
        """执行 Tavily 搜索。

        Args:
            query: 搜索查询字符串。
            max_results: 最大结果数，默认使用实例配置。
            search_depth: 搜索深度，默认使用实例配置。
            include_answer: 是否包含 AI 生成的摘要。
            include_raw_content: 是否包含原始页面内容。

        Returns:
            Tavily API 返回的字典，包含：
              - query: 查询字符串
              - answer: AI 摘要（若 include_answer=True）
              - results: 结果列表，每项含 title/url/content/score
              - response_time: 响应时间

            若搜索不可用或出错，返回 {"results": [], "answer": "", "error": "..."}。
        """
        if not self.available:
            return {"results": [], "answer": "", "error": "API key not configured"}

        client = self._get_client()
        if client is None:
            return {"results": [], "answer": "", "error": "client init failed"}

        try:
            result = client.search(
                query=query,
                max_results=max_results or self.max_results,
                search_depth=search_depth or self.search_depth,
                include_answer=include_answer,
                include_raw_content=include_raw_content,
            )
            return result
        except Exception as e:
            print(f"[tavily] 搜索失败: {e}")
            return {"results": [], "answer": "", "error": str(e)}


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def create_search_tool(**kwargs: Any) -> TavilySearchTool:
    """从环境变量创建搜索工具的便捷函数。"""
    return TavilySearchTool.from_env(**kwargs)
