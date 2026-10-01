"""识别同一 Todo 内参数完全相同的重复工具调用。

与 ``FailureTracker`` 互补：失败熔断只处理「同样的失败反复出现」，
而模型在成功之后重复调用同一工具（同一参数）同样不会带来新信息，
却会消耗时间、上下文和决策预算，因此这里直接拦截，不再执行。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..agent.loop import Action
from ..agent.state import Observation, summarize_tool_payload

__all__ = ["MUTATING_TOOLS", "RepeatGuard", "RepeatNotice"]

# 成功后会改变外部状态的工具：此后允许再次读取同一目标（例如改完文件再复核）
MUTATING_TOOLS = frozenset(
    {
        "write_artifact",
        "create_workspace_file",
        "edit_workspace_file",
        "execute_python",
        "run_project_command",
    }
)


@dataclass(frozen=True)
class RepeatNotice:
    """一次被拦截的重复调用。"""

    tool_name: str
    count: int
    previous_digest: str | None

    def message(self) -> str:
        """生成给模型阅读的拦截说明。"""
        detail = (
            f"上一次相同调用的结果要点：{self.previous_digest}。"
            if self.previous_digest
            else "上一次相同调用的结果已在本轮 observations 中。"
        )
        return (
            f"重复调用已拦截（第 {self.count} 次相同调用）：本次工具名与参数"
            f"与之前完全相同，未再次执行。{detail}"
            "请直接使用已有结果继续判断并返回 finish；"
            "确需更多内容时，请更换参数（如 offset/limit、行范围、筛选条件）或改用其他工具。"
        )


@dataclass
class RepeatGuard:
    """按「工具名 + 参数」指纹识别重复调用，状态被修改后自动失效。"""

    _counts: dict[str, int] = field(default_factory=dict)
    _digests: dict[str, str] = field(default_factory=dict)
    _blocked: dict[str, int] = field(default_factory=dict)

    def check(self, action: Action) -> RepeatNotice | None:
        """判断本次调用是否与已成功的调用完全重复。

        Args:
            action: 待执行的决策。

        Returns:
            重复时返回拦截说明，并累计出现次数；否则返回 None。
        """
        fingerprint = _fingerprint(action)
        if fingerprint not in self._counts:
            return None
        count = self._counts[fingerprint] + 1
        self._counts[fingerprint] = count
        self._blocked[action.tool_name] = self._blocked.get(action.tool_name, 0) + 1
        return RepeatNotice(
            tool_name=action.tool_name,
            count=count,
            previous_digest=self._digests.get(fingerprint),
        )

    def record(self, action: Action, observation: Observation) -> None:
        """记录一次真实执行；成功写入类工具会清空此前的重复判定。"""
        if not observation.success:
            return
        if action.tool_name in MUTATING_TOOLS:
            # 外部状态可能已变化，之前的「已读过/已列过」不再成立
            self._counts.clear()
            self._digests.clear()
        fingerprint = _fingerprint(action)
        self._counts[fingerprint] = self._counts.get(fingerprint, 0) + 1
        digest = _result_digest(observation.result)
        if digest:
            self._digests[fingerprint] = digest

    def blocked_count(self) -> int:
        """返回本次 Todo 内被拦截的重复调用总数。"""
        return sum(self._blocked.values())

    def blocked_summary(self) -> str:
        """按工具汇总被拦截的重复调用，供失败摘要与恢复规划使用。"""
        return ", ".join(
            f"{tool} ×{count}" for tool, count in sorted(self._blocked.items())
        )


def _fingerprint(action: Action) -> str:
    """构造与参数顺序无关的调用指纹。"""
    payload = json.dumps(
        {"tool_name": action.tool_name, "arguments": action.arguments},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _result_digest(result: Any) -> str | None:
    """为重复调用提示准备一段简短的结果要点。"""
    summary = summarize_tool_payload(result)
    if summary:
        return summary
    if isinstance(result, str) and result.strip():
        text = result.strip()
        return text[:200] + ("…" if len(text) > 200 else "")
    return None
