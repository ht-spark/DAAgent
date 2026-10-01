"""附件上下文编译的边界测试。"""

from __future__ import annotations

from data_analysis.context.attachments import (
    ATTACHMENT_RAW_CHARS,
    AttachmentText,
    compile_attachment_context,
    compose_user_message,
    message_with_attachment_context,
)


class SummaryLlm:
    """记录附件摘要输入并返回固定结果。"""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    def invoke(self, messages: list[tuple[str, str]]):
        """返回可识别的详细摘要。"""
        self.calls.append(messages)
        return type("Response", (), {"content": "保留数字 123 与限制条件。"})()


def test_short_attachment_is_loaded_in_full_without_llm() -> None:
    llm = SummaryLlm()

    context = compile_attachment_context(
        [AttachmentText(name="notes.md", text="完整附件内容")],
        llm,
    )

    assert "完整附件内容" in context
    assert "### 全文" in context
    assert llm.calls == []


def test_long_attachment_keeps_prefix_and_summarizes_only_overflow() -> None:
    llm = SummaryLlm()
    prefix = "甲" * ATTACHMENT_RAW_CHARS
    overflow = "仅存在于超出部分 123"

    context = compile_attachment_context(
        [AttachmentText(name="report.txt", text=prefix + overflow)],
        llm,
    )

    assert prefix in context
    assert overflow not in context
    assert "保留数字 123 与限制条件" in context
    assert len(llm.calls) == 1
    assert overflow in llm.calls[0][1][1]
    assert prefix not in llm.calls[0][1][1]


def test_persisted_attachment_context_is_restored_into_user_message() -> None:
    entry = {
        "seq": 1,
        "role": "user",
        "content": "分析附件",
        "metadata": {"attachment_context": "附件详细内容"},
    }

    enriched = message_with_attachment_context(entry)

    assert enriched is not entry
    assert "分析附件" in enriched["content"]
    assert "附件详细内容" in enriched["content"]
    assert "<attachment_context>" in enriched["content"]
    assert compose_user_message("继续", None) == "继续"
