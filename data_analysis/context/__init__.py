"""单会话上下文编译与滚动记忆。"""

from .attachments import (
    ATTACHMENT_RAW_CHARS,
    AttachmentText,
    compile_attachment_context,
    compose_user_message,
)
from .rolling_memory import (
    COMPACTION_TRIGGER_TOKENS,
    DEFAULT_MODEL_CONTEXT_TOKENS,
    KEEP_RECENT_TOKENS,
    CompactionRecord,
    RollingContext,
    compile_rolling_context,
    estimate_context_usage_after_turn,
    estimate_text_tokens,
    resolve_compaction_trigger,
    resolve_keep_recent_tokens,
)

__all__ = [
    "ATTACHMENT_RAW_CHARS",
    "AttachmentText",
    "COMPACTION_TRIGGER_TOKENS",
    "DEFAULT_MODEL_CONTEXT_TOKENS",
    "KEEP_RECENT_TOKENS",
    "CompactionRecord",
    "RollingContext",
    "compile_rolling_context",
    "compile_attachment_context",
    "compose_user_message",
    "estimate_context_usage_after_turn",
    "estimate_text_tokens",
    "resolve_compaction_trigger",
    "resolve_keep_recent_tokens",
]
