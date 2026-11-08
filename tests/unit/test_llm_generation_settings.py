"""LLM 生成参数传递测试（无网络）。"""
from __future__ import annotations

import sys
from types import ModuleType

import pytest

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


@pytest.mark.parametrize("setting, expected", [("true", True), ("false", False), ("auto", None)])
def test_stream_usage_configuration_preserves_provider_compatibility(monkeypatch, setting, expected):
    """流式计量可显式开关，默认由客户端决定供应商兼容行为。"""
    captured = {}
    fake_module = ModuleType("langchain_openai")
    fake_module.ChatOpenAI = lambda **kwargs: captured.update(kwargs)
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)
    monkeypatch.setenv("LLM_STREAM_USAGE", setting)
    create_llm(provider="custom", api_key="test", base_url="https://example.invalid/v1", model="mock", fallback_env=False)
    if expected is None:
        assert "stream_usage" not in captured
    else:
        assert captured["stream_usage"] is expected
