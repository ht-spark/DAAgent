"""LLM 生成参数传递测试（无网络）。"""
from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType

from scr.math_modeling_agent.llm import create_llm
from server import runs


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
        fallback_env=False,
    )

    assert isinstance(llm, FakeChatOpenAI)
    assert captured["max_tokens"] == 3072
    assert captured["temperature"] == 0.0


def test_modeling_run_forwards_generation_settings(monkeypatch, tmp_path):
    """建模执行器必须把表单已校验的生成参数传给 LLM 工厂。"""
    captured: dict[str, object] = {}
    budget_event = threading.Event()
    budget_event.set()

    def fake_create_llm(**kwargs):
        captured.update(kwargs)
        return "modeling-llm"

    monkeypatch.setattr(runs, "create_llm", fake_create_llm)
    monkeypatch.setattr(runs, "mark_running", lambda _run_id: None)
    monkeypatch.setattr(runs, "append_progress", lambda _run_id, _event: None)
    monkeypatch.setattr(runs, "request_budget_confirmation", lambda *_args: budget_event)
    monkeypatch.setattr(runs, "take_budget_decision", lambda _run_id: None)
    monkeypatch.setattr(runs, "clear_budget_pending", lambda _run_id: None)
    monkeypatch.setattr(runs, "run_graph", lambda **_kwargs: {})
    monkeypatch.setattr(runs, "set_result", lambda *_args: None)

    asyncio.run(
        runs.execute_run(
            run_id="run-test",
            problem_text="test problem",
            data_paths=[],
            output_dir=str(tmp_path),
            model_config={
                "provider": "custom",
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "model": "test-model",
                "max_tokens": 4096,
                "temperature": 0.25,
            },
        )
    )

    assert captured["max_tokens"] == 4096
    assert captured["temperature"] == 0.25
