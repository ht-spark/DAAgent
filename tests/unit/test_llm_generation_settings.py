"""LLM 生成参数传递测试（无网络）。"""
from __future__ import annotations

import sys
from types import ModuleType

from data_analysis.llm import create_llm


def test_create_llm_uses_explicit_generation_settings(monkeypatch):
    """建模任务传入的 token 上限与温度不能被默认值覆盖。"""
    captured: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = ModuleType("langchain_openai")
    fake_module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)

    llm = create_llm(
        provider="custom",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="test-model",
        max_tokens=3072,
        temperature=0.0,
        reasoning_effort="xhigh",
        fallback_env=False,
    )

    assert isinstance(llm, FakeChatOpenAI)
    assert captured["max_tokens"] == 3072
    assert captured["temperature"] == 0.0
    assert captured["reasoning_effort"] == "xhigh"


def test_create_llm_omits_reasoning_effort_when_close(monkeypatch):
    """关闭思考程度时不得向端点透传 reasoning_effort。"""
    captured: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = ModuleType("langchain_openai")
    fake_module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)

    llm = create_llm(
        provider="custom",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="test-model",
        reasoning_effort="close",
        fallback_env=False,
    )

    assert isinstance(llm, FakeChatOpenAI)
    assert "reasoning_effort" not in captured
