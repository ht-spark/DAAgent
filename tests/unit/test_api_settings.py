"""API 配置本地持久化端点测试（无 LLM、无网络）。"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from server import main


def _use_tmp_settings(monkeypatch, tmp_path):
    path = tmp_path / "api_settings.json"
    monkeypatch.setattr(main, "API_SETTINGS_PATH", path)
    return path


def test_get_returns_unsaved_when_no_file(monkeypatch, tmp_path):
    _use_tmp_settings(monkeypatch, tmp_path)
    result = asyncio.run(main.get_api_settings_endpoint())
    assert result["saved"] is False
    assert result["configs"] == []
    assert result["active_id"] is None


def test_put_then_get_roundtrip(monkeypatch, tmp_path):
    path = _use_tmp_settings(monkeypatch, tmp_path)
    snapshot = {
        "configs": [
            {
                "id": "cfg_1",
                "name": "DeepSeek",
                "provider": "deepseek",
                "apiKey": "sk-test",
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "maxTokens": 4096,
                "temperature": 0.2,
                "createdAt": 1,
            }
        ],
        "active_id": "cfg_1",
        "external_services": {"tavily": {"apiKey": "tvly-x", "baseUrl": ""}},
    }

    asyncio.run(main.save_api_settings_endpoint(snapshot))
    assert path.exists()

    result = asyncio.run(main.get_api_settings_endpoint())
    assert result["saved"] is True
    assert result["configs"][0]["id"] == "cfg_1"
    assert result["configs"][0]["maxTokens"] == 4096
    assert result["configs"][0]["temperature"] == 0.2
    assert result["active_id"] == "cfg_1"
    assert result["external_services"]["tavily"]["apiKey"] == "tvly-x"


def test_put_rejects_malformed_configs(monkeypatch, tmp_path):
    path = _use_tmp_settings(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.save_api_settings_endpoint({"configs": "not-a-list"}))
    assert exc.value.status_code == 422
    assert not path.exists()


def test_put_sanitizes_non_string_active_id(monkeypatch, tmp_path):
    path = _use_tmp_settings(monkeypatch, tmp_path)
    asyncio.run(
        main.save_api_settings_endpoint({"configs": [], "active_id": 123, "external_services": None})
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["active_id"] is None
    assert data["external_services"] == {}


def test_get_survives_corrupt_file(monkeypatch, tmp_path):
    path = _use_tmp_settings(monkeypatch, tmp_path)
    path.write_text("{broken json", encoding="utf-8")
    result = asyncio.run(main.get_api_settings_endpoint())
    assert result["saved"] is False


def test_active_discussion_llm_uses_saved_generation_settings(monkeypatch, tmp_path):
    """头脑风暴必须将当前 API 配置的生成参数传给 LLM 工厂。"""
    _use_tmp_settings(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_create_llm(**kwargs):
        captured.update(kwargs)
        return "discussion-llm"

    monkeypatch.setattr(main, "create_llm", fake_create_llm)
    asyncio.run(
        main.save_api_settings_endpoint(
            {
                "configs": [
                    {
                        "id": "cfg_1",
                        "provider": "custom",
                        "apiKey": "test-key",
                        "baseUrl": "https://example.invalid/v1",
                        "model": "test-model",
                        "maxTokens": 3072,
                        "temperature": 0.1,
                        "reasoningEffort": "max",
                    }
                ],
                "active_id": "cfg_1",
            }
        )
    )

    assert main._active_discussion_llm() == "discussion-llm"
    assert captured["max_tokens"] == 3072
    assert captured["temperature"] == 0.1
    assert captured["reasoning_effort"] == "max"
    assert captured["fallback_env"] is False


def test_discussion_llm_by_id_selects_requested_config(monkeypatch, tmp_path):
    """输入框选中的 config_id 必须覆盖默认激活配置。"""
    _use_tmp_settings(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_create_llm(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return f"llm-{kwargs.get('model')}"

    monkeypatch.setattr(main, "create_llm", fake_create_llm)
    asyncio.run(
        main.save_api_settings_endpoint(
            {
                "configs": [
                    {
                        "id": "cfg_default",
                        "provider": "openai",
                        "apiKey": "key-default",
                        "baseUrl": "https://default.invalid/v1",
                        "model": "default-model",
                        "maxTokens": 1000,
                        "temperature": 0.2,
                    },
                    {
                        "id": "cfg_picked",
                        "provider": "custom",
                        "apiKey": "key-picked",
                        "baseUrl": "https://picked.invalid/v1",
                        "model": "picked-model",
                        "maxTokens": 2000,
                        "temperature": 0.7,
                    },
                ],
                "active_id": "cfg_default",
            }
        )
    )

    assert main._discussion_llm_by_id("cfg_picked") == "llm-picked-model"
    assert captured["api_key"] == "key-picked"
    assert captured["model"] == "picked-model"

    assert main._discussion_llm_by_id(None) == "llm-default-model"
    assert captured["model"] == "default-model"

    assert main._discussion_llm_by_id("missing") is None
