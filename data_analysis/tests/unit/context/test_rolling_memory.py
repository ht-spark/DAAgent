from __future__ import annotations

from data_analysis.agent import AgentLoop, AgentMessage, AgentState
from data_analysis.context import compile_rolling_context, estimate_context_usage_after_turn

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
