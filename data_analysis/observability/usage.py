"""兼容供应商响应的用量提取；未知计数始终保留为空。"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def extract_usage(response: Any) -> tuple[int | None, int | None]:
    """提取输入、输出 token，逐字段回退供应商原始计量。

    Args:
        response: 原始响应或流式响应片段。

    Returns:
        输入与输出计数；缺失或异常字段返回 None。
    """
    usage = getattr(response, "usage_metadata", None)
    usage = usage if isinstance(usage, Mapping) else {}
    metadata = getattr(response, "response_metadata", None)
    fallback = metadata.get("token_usage") if isinstance(metadata, Mapping) else None
    fallback = fallback if isinstance(fallback, Mapping) else {}
    input_tokens = _count(usage.get("input_tokens"))
    output_tokens = _count(usage.get("output_tokens"))
    return (
        input_tokens if input_tokens is not None else _count(fallback.get("prompt_tokens")),
        output_tokens if output_tokens is not None else _count(fallback.get("completion_tokens")),
    )


class TokenPricing(BaseModel):
    """当前客户端每百万 token 的美元单价，由配置提供，不内置模型价目。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 输入单价默认不配置；提高此值会同比提高输入成本估算，不影响模型行为。
    input_per_million: float = Field(ge=0, allow_inf_nan=False)
    # 输出单价默认不配置；提高此值会同比提高输出成本估算，不影响模型行为。
    output_per_million: float = Field(ge=0, allow_inf_nan=False)

    def estimate(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        """仅在两个计数均已知时估算成本。"""
        if input_tokens is None or output_tokens is None:
            return None
        return (input_tokens * self.input_per_million + output_tokens * self.output_per_million) / 1_000_000
