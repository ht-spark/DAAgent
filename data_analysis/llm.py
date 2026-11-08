"""统一 Agent 使用的 OpenAI 兼容 LLM 客户端工厂。"""

from __future__ import annotations

import logging
import os
from typing import Any


LOGGER = logging.getLogger(__name__)
LLM_CALL_TIMEOUT_SECONDS = 180


def create_llm(
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    fallback_env: bool = True,
) -> Any | None:
    """创建 OpenAI 兼容的 LLM 客户端。

    Args:
        provider: 服务商标识，可为 openai、deepseek、custom 或 None。
        api_key: API 密钥，缺省时可回退环境变量。
        base_url: 自定义 API 地址。
        model: 模型名称。
        max_tokens: 单次回复的最大输出 token 数。
        temperature: 生成随机性。
        reasoning_effort: 思考程度；close、off、none 或空值时不传。
        fallback_env: 显式参数缺失时是否允许使用环境变量。

    Returns:
        ChatOpenAI 客户端；配置或依赖不可用时返回 None。
    """
    resolved_provider = provider
    resolved_key = api_key
    resolved_url = base_url
    resolved_model = model

    generic_provider = os.getenv("LLM_PROVIDER") if fallback_env else None
    generic_key = os.getenv("LLM_API_KEY") if fallback_env else None
    generic_url = os.getenv("LLM_BASE_URL") if fallback_env else None
    generic_model = os.getenv("LLM_MODEL") if fallback_env else None
    openai_key = os.getenv("OPENAI_API_KEY") if fallback_env else None
    deepseek_key = os.getenv("DEEPSEEK_API_KEY") if fallback_env else None

    if resolved_provider is None:
        if generic_provider:
            resolved_provider = generic_provider
        elif generic_key:
            resolved_provider = "custom"
        elif openai_key:
            resolved_provider = "openai"
        elif deepseek_key:
            resolved_provider = "deepseek"
        else:
            resolved_provider = "openai"

    if resolved_provider == "openai":
        resolved_key = resolved_key or openai_key
        resolved_model = resolved_model or generic_model or os.getenv("MODEL_NAME", "gpt-4o")
        resolved_url = resolved_url or generic_url or os.getenv("OPENAI_BASE_URL")
    elif resolved_provider == "deepseek":
        resolved_key = resolved_key or deepseek_key
        resolved_model = resolved_model or generic_model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        resolved_url = resolved_url or generic_url or os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        )
    elif resolved_provider == "custom":
        resolved_key = resolved_key or generic_key
        resolved_url = resolved_url or generic_url
        resolved_model = resolved_model or generic_model
        if not (resolved_key and resolved_url and resolved_model):
            return None
    else:
        return None

    if not resolved_key:
        return None

    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        LOGGER.warning("langchain_openai 未安装，LLM 不可用")
        return None

    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "api_key": resolved_key,
        "timeout": LLM_CALL_TIMEOUT_SECONDS,
        "temperature": 0.3 if temperature is None else temperature,
        "max_tokens": 20_000 if max_tokens is None else max_tokens,
    }
    # 默认 auto 沿用客户端的供应商兼容策略；true 请求流式 token 尾片段，
    # false 禁用该请求。启用可补齐自定义端点计量，但不支持的端点可能拒绝请求。
    stream_usage = os.getenv("LLM_STREAM_USAGE", "auto").strip().lower()
    if stream_usage in {"true", "false"}:
        kwargs["stream_usage"] = stream_usage == "true"
    if resolved_url:
        kwargs["base_url"] = resolved_url
    if reasoning_effort and reasoning_effort not in {"close", "off", "none"}:
        kwargs["reasoning_effort"] = reasoning_effort
    return ChatOpenAI(**kwargs)
