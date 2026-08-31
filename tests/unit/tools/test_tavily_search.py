"""Tavily 搜索工具单元测试。

测试覆盖：
  1. 工具创建与可用性检查
  2. 通用搜索方法（mocked，不实际调用 API）
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from scr.tools.tavily_search import TavilySearchTool, create_search_tool


class TestTavilySearchToolCreation:
    """测试搜索工具的创建和可用性检查。"""

    def test_create_with_api_key(self):
        """有 API key 时工具可用。"""
        tool = TavilySearchTool(api_key="test-key")
        assert tool.available is True

    def test_create_without_api_key(self):
        """无 API key 时工具不可用。"""
        tool = TavilySearchTool(api_key=None)
        assert tool.available is False

    def test_create_with_empty_api_key(self):
        """空 API key 时工具不可用。"""
        tool = TavilySearchTool(api_key="")
        assert tool.available is False

    def test_from_env_with_key(self, monkeypatch):
        """从环境变量创建工具（有 key）。"""
        monkeypatch.setenv("TAVILY_API_KEY", "env-test-key")
        tool = TavilySearchTool.from_env()
        assert tool.available is True
        assert tool.api_key == "env-test-key"

    def test_from_env_with_mixed_case_key(self, monkeypatch):
        """从环境变量创建工具（混合大小写 key 名）。

        注意：Windows 上环境变量名大小写不敏感，TAVILY_API_KEY 和
        Tavily_API_KEY 是同一个变量。此测试验证 from_env 能正确读取。
        """
        monkeypatch.setenv("Tavily_API_KEY", "mixed-case-key")
        tool = TavilySearchTool.from_env()
        assert tool.available is True
        assert tool.api_key == "mixed-case-key"

    def test_from_env_without_key(self, monkeypatch):
        """从环境变量创建工具（无 key）。"""
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("Tavily_API_KEY", raising=False)
        monkeypatch.delenv("tavily_api_key", raising=False)
        tool = TavilySearchTool.from_env()
        assert tool.available is False

    def test_create_search_tool_factory(self, monkeypatch):
        """工厂函数创建工具。"""
        monkeypatch.setenv("TAVILY_API_KEY", "factory-key")
        tool = create_search_tool()
        assert tool.available is True


# ---------------------------------------------------------------------------
# 搜索方法（mocked）
# ---------------------------------------------------------------------------


class TestTavilySearch:
    """测试搜索方法（mocked TavilyClient）。"""

    def test_search_unavailable_returns_empty(self):
        """无 API key 时搜索返回空结果。"""
        tool = TavilySearchTool(api_key=None)
        result = tool.search("test query")
        assert result["results"] == []
        assert "error" in result

    @patch("scr.tools.tavily_search.TavilySearchTool._get_client")
    def test_search_success(self, mock_get_client):
        """模拟搜索成功。"""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "test",
            "answer": "AI answer",
            "results": [
                {"title": "Result 1", "url": "https://example.com/1", "content": "Content 1", "score": 0.95},
                {"title": "Result 2", "url": "https://example.com/2", "content": "Content 2", "score": 0.80},
            ],
            "response_time": 1.5,
        }
        mock_get_client.return_value = mock_client

        tool = TavilySearchTool(api_key="test-key")
        result = tool.search("test query")

        assert len(result["results"]) == 2
        assert result["answer"] == "AI answer"
        mock_client.search.assert_called_once()

    @patch("scr.tools.tavily_search.TavilySearchTool._get_client")
    def test_search_error_returns_empty(self, mock_get_client):
        """模拟搜索失败时优雅降级。"""
        mock_client = MagicMock()
        mock_client.search.side_effect = Exception("Network error")
        mock_get_client.return_value = mock_client

        tool = TavilySearchTool(api_key="test-key")
        result = tool.search("test query")

        assert result["results"] == []
        assert "error" in result
