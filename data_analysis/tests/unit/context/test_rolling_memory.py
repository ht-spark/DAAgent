from __future__ import annotations

import pytest

from data_analysis.agent import AgentLoop, AgentMessage, AgentState
from data_analysis.context import (
    COMPACTION_TRIGGER_TOKENS,
    KEEP_RECENT_TOKENS,
    compile_rolling_context,
    estimate_context_usage_after_turn,
    resolve_compaction_trigger,
    resolve_keep_recent_tokens,
)
from data_analysis.context import rolling_memory

NL = chr(10)


class SummaryLlm:
    """记录压缩输入并返回固定结构化摘要。"""

    def __init__(self, summaries: list[str] | None = None) -> None:
        self.summaries = iter(summaries or ["## Goal" + NL + "保留历史目标"])
        self.calls = []

    def invoke(self, messages):
        """记录消息并返回下一份摘要。"""
        self.calls.append(messages)
        return type("Response", (), {"content": next(self.summaries)})()


class FailingSummaryLlm:
    """模拟摘要模型调用失败。"""

    def invoke(self, messages):
        """抛出供应商错误。"""
        raise RuntimeError("provider timeout")


def _message(seq: int, role: str, content: str) -> dict:
    return {
        "seq": seq,
        "role": role,
        "content": content,
        "metadata": {},
    }


def test_estimate_context_usage_after_turn_matches_next_history_budget() -> None:
    """本轮用量应计入摘要、已有消息及本轮用户和助手消息。"""
    usage = estimate_context_usage_after_turn(
        "summary",
        [_message(1, "user", "old")],
        "new",
        "answer",
        trigger_tokens=100,
    )

    assert usage == {
        "estimated_tokens": 30,
        "threshold_tokens": 100,
        "remaining_tokens": 70,
    }


def test_estimate_context_usage_after_turn_clamps_remaining_to_zero() -> None:
    """超过压缩阈值时不能显示负的剩余 token。"""
    usage = estimate_context_usage_after_turn(None, [], "request", "response", trigger_tokens=10)

    assert usage["estimated_tokens"] > 10
    assert usage["remaining_tokens"] == 0


def test_short_session_keeps_all_raw_messages_without_summary_call() -> None:
    """未达到阈值时不应进行无意义压缩。"""
    entries = [
        _message(1, "user", "分析销量"),
        _message(2, "assistant", "请上传数据"),
    ]
    llm = SummaryLlm()

    context = compile_rolling_context(
        entries,
        llm,
        trigger_tokens=100,
        keep_recent_tokens=40,
    )

    assert context.memory_summary is None
    assert [item["seq"] for item in context.recent_messages] == [1, 2]
    assert context.compaction is None
    assert llm.calls == []


def test_recent_message_restores_persisted_attachment_context() -> None:
    """附件上下文应随用户消息进入后续轮次，而不是依赖原文件。"""
    entry = _message(1, "user", "分析附件")
    entry["metadata"] = {"attachment_context": "附件字段：sales、amount"}

    context = compile_rolling_context([entry], SummaryLlm(), trigger_tokens=100)

    assert len(context.recent_messages) == 1
    assert "附件字段：sales、amount" in context.recent_messages[0]["content"]


def test_long_session_compacts_old_turns_and_keeps_recent_raw_turn() -> None:
    """长会话应保留最近完整轮次并压缩更早历史。"""
    entries = [
        _message(1, "user", "甲" * 20),
        _message(2, "assistant", "乙" * 20),
        _message(3, "user", "丙" * 20),
        _message(4, "assistant", "丁" * 20),
        _message(5, "user", "戊" * 20),
        _message(6, "assistant", "己" * 20),
    ]
    llm = SummaryLlm(["## Goal" + NL + "分析销量" + NL * 2 + "## Next Steps" + NL + "继续验证"])

    context = compile_rolling_context(
        entries,
        llm,
        trigger_tokens=100,
        keep_recent_tokens=60,
    )

    assert context.memory_summary.startswith("## Goal")
    assert [item["seq"] for item in context.recent_messages] == [5, 6]
    assert context.compaction is not None
    assert context.compaction.covered_through_seq == 4
    assert context.compaction.metadata()["kind"] == "agent_compaction"
    assert "[USER seq=1]" in llm.calls[0][1][1]


def test_recursive_compaction_hands_previous_summary_forward() -> None:
    """后续压缩应把旧摘要与新变旧的原文递归合并。"""
    entries = [
        _message(1, "user", "旧问题"),
        _message(2, "assistant", "旧回答"),
        {
            "seq": 3,
            "role": "tool",
            "content": "## Goal" + NL + "旧目标",
            "metadata": {
                "kind": "agent_compaction",
                "covered_through_seq": 2,
            },
        },
        _message(4, "user", "甲" * 30),
        _message(5, "assistant", "乙" * 30),
        _message(6, "user", "丙" * 30),
        _message(7, "assistant", "丁" * 30),
    ]
    llm = SummaryLlm(["## Goal" + NL + "合并后的目标"])

    context = compile_rolling_context(
        entries,
        llm,
        trigger_tokens=90,
        keep_recent_tokens=80,
    )

    assert context.compaction is not None
    assert context.compaction.covered_through_seq == 5
    assert [item["seq"] for item in context.recent_messages] == [6, 7]
    assert "<previous_summary>" + NL + "## Goal" + NL + "旧目标" in llm.calls[0][1][1]


def test_compaction_failure_keeps_bounded_recent_context() -> None:
    """摘要失败不阻断请求，也不退回无限完整历史。"""
    entries = [
        _message(1, "user", "甲" * 20),
        _message(2, "assistant", "乙" * 20),
        _message(3, "user", "丙" * 20),
        _message(4, "assistant", "丁" * 20),
        _message(5, "user", "戊" * 20),
        _message(6, "assistant", "己" * 20),
    ]

    context = compile_rolling_context(
        entries,
        FailingSummaryLlm(),
        trigger_tokens=100,
        keep_recent_tokens=60,
    )

    assert context.compaction is None
    assert context.compaction_error == "provider timeout"
    assert [item["seq"] for item in context.recent_messages] == [5, 6]

def test_agent_context_contains_summary_before_recent_raw_messages() -> None:
    """模型上下文应按“滚动摘要 + 最近原文 + 当前状态”排列。"""
    state = AgentState(
        user_query="继续分析",
        memory_summary="## Goal" + NL + "分析历史销量",
        messages=[
            AgentMessage(role="user", content="最近问题"),
            AgentMessage(role="assistant", content="最近回答"),
        ],
    )

    messages = AgentLoop(
        SummaryLlm(),
        [],
        "系统规则",
    ).build_messages(state)

    assert messages[0][0] == "system"
    assert "<session_memory>" in messages[1][1]
    assert "分析历史销量" in messages[1][1]
    assert messages[2:] == [
        ("user", "最近问题"),
        ("assistant", "最近回答"),
        messages[-1],
    ]


def test_resolve_compaction_trigger_derives_from_context_window() -> None:
    """阈值应按模型窗口减去输出与固定开销推导，并受上下限约束。"""
    # 128k 窗口：128000 - 32000 - 24000 = 72000
    assert resolve_compaction_trigger(128_000) == 72_000
    # 超大窗口仍受既有阈值上限约束
    assert resolve_compaction_trigger(1_000_000) == COMPACTION_TRIGGER_TOKENS
    # 极小窗口回落到下限，避免每轮都压缩
    assert resolve_compaction_trigger(1_000) == 16_000
    # 未配置时使用保守默认窗口
    assert resolve_compaction_trigger(None) == resolve_compaction_trigger(
        rolling_memory.DEFAULT_MODEL_CONTEXT_TOKENS
    )
    assert resolve_compaction_trigger(0) == resolve_compaction_trigger(None)


def test_default_compaction_window_is_300k() -> None:
    """未配置模型窗口时，界面与滚动记忆都应使用 300K 压缩阈值。"""
    assert COMPACTION_TRIGGER_TOKENS == 300_000
    assert resolve_compaction_trigger(None) == 300_000


def test_resolve_keep_recent_tokens_scales_with_trigger() -> None:
    """保留最近原文的预算应随阈值缩放，并在默认阈值下保持既有取值。"""
    assert resolve_keep_recent_tokens(200_000) == KEEP_RECENT_TOKENS
    assert resolve_keep_recent_tokens(72_000) == 14_400
    assert resolve_keep_recent_tokens(10_000) == 8_000
    with pytest.raises(ValueError, match="必须大于 0"):
        resolve_keep_recent_tokens(0)


def test_estimate_context_usage_uses_derived_trigger() -> None:
    """未显式给出阈值时应与编译滚动上下文使用同一推导口径。"""
    usage = estimate_context_usage_after_turn(
        None,
        [],
        "问",
        "答",
        context_window_tokens=128_000,
    )

    assert usage["threshold_tokens"] == 72_000


def test_long_history_is_summarized_in_batches_without_truncation(monkeypatch) -> None:
    """待压缩原文超单批上限时应分批递归摘要，而不是截断丢弃早期内容。"""
    monkeypatch.setattr(rolling_memory, "_MAX_COMPACTION_INPUT_CHARS", 120)
    entries = [
        _message(index, "user" if index % 2 else "assistant", f"第{index}段内容" + "甲" * 40)
        for index in range(1, 9)
    ]
    llm = SummaryLlm([f"## Goal{NL}第{index}批" for index in range(1, 9)])

    context = compile_rolling_context(
        entries,
        llm,
        trigger_tokens=10,
        keep_recent_tokens=10,
    )

    assert context.compaction is not None
    assert context.compaction.covered_through_seq == 8
    assert context.memory_summary == "## Goal" + NL + "第8批"
    # 每批一次调用，且后续批次把上一批摘要递归带上
    assert len(llm.calls) > 1
    assert "第1批" in llm.calls[1][1][1]
    # 最早的原文只出现在第一批，不会被整段截断丢弃
    assert "第1段内容" in llm.calls[0][1][1]


def test_single_long_attachment_context_is_split_without_data_loss(monkeypatch) -> None:
    """单条附件上下文超过摘要批次上限时，中间内容也必须进入模型输入。"""
    monkeypatch.setattr(rolling_memory, "_MAX_COMPACTION_INPUT_CHARS", 120)
    attachment_context = (
        "附件开头标记"
        + "甲" * 100
        + "附件中间标记"
        + "乙" * 100
        + "附件结尾标记"
    )
    entries = [
        {
            "seq": 1,
            "role": "user",
            "content": "分析附件",
            "metadata": {"attachment_context": attachment_context},
        }
    ]
    llm = SummaryLlm([f"## Goal{NL}第{index}批" for index in range(1, 20)])

    context = compile_rolling_context(
        entries,
        llm,
        trigger_tokens=10,
        keep_recent_tokens=10,
    )

    prompts = "\n".join(call[1][1] for call in llm.calls)
    assert context.compaction is not None
    assert len(llm.calls) > 1
    assert "附件开头标记" in prompts
    assert "附件中间标记" in prompts
    assert "附件结尾标记" in prompts
