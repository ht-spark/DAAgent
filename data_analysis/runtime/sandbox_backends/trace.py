"""沙箱执行审计事件（对齐设计方案 §8.3 的 Trace 字段骨架）。

只记录标识、后端、策略摘要与状态字段；**禁止**记录用户代码、数据内容、
密钥或授权文件原文。敏感排障需另行设计受控日志机制。
"""

from __future__ import annotations

import json
import logging

__all__ = ["emit_sandbox_event"]

_logger = logging.getLogger("data_analysis.sandbox")


def emit_sandbox_event(event: str, **fields: object) -> None:
    """输出一条结构化沙箱事件。

    Args:
        event: 事件名（短标识，如 ``mxc.run.start``）。
        **fields: 标量字段；不可序列化对象经 ``default=str`` 转换，不抛异常。
    """
    try:
        detail = json.dumps(fields, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        detail = "{}"
    _logger.info("sandbox.%s %s", event, detail)
