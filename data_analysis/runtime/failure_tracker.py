"""识别没有新增信息的重复工具失败。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from ..agent.loop import Action
from ..agent.state import Observation

__all__ = ["FailureCircuitOpen", "FailureTracker", "RepeatedToolFailureError"]


class RepeatedToolFailureError(RuntimeError):
    """相同工具调用重复失败且没有形成新信息。"""


@dataclass(frozen=True)
class FailureCircuitOpen:
    """一次达到熔断阈值的失败摘要。"""

    fingerprint: str
    tool_name: str
    count: int
    error: str


@dataclass
class FailureTracker:
    """按工具、参数和规范化错误构造失败指纹并累计次数。"""

    threshold: int = 3
    _counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.threshold < 2:
            raise ValueError("失败熔断阈值必须大于等于 2")

    def record(
        self,
        action: Action,
        observation: Observation,
    ) -> FailureCircuitOpen | None:
        """记录一次观察；成功时不计数，达到阈值时返回熔断信息。"""
        if observation.success:
            return None
        normalized_error = _normalize_error(observation.error or "工具执行失败")
        payload = json.dumps(
            {
                "tool_name": action.tool_name,
                "arguments": action.arguments,
                "error": normalized_error,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        count = self._counts.get(fingerprint, 0) + 1
        self._counts[fingerprint] = count
        if count < self.threshold:
            return None
        return FailureCircuitOpen(
            fingerprint=fingerprint,
            tool_name=action.tool_name,
            count=count,
            error=normalized_error,
        )


def _normalize_error(error: str) -> str:
    """移除易变化的地址和纯数字，避免同一错误被随机值拆散。"""
    text = re.sub(r"0x[0-9a-fA-F]+", "<addr>", error.strip())
    text = re.sub(r"\b\d{4,}\b", "<number>", text)
    return re.sub(r"\s+", " ", text)[:1000]