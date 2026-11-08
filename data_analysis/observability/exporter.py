"""结束的 span 双写本地 JSONL 与宿主提供的 SQLite 存储。"""

import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path

from .trace import Span

LOGGER = logging.getLogger(__name__)


class LocalTraceExporter:
    """两个写入通道独立尝试；单侧故障不阻断另一侧或分析任务。"""

    def __init__(self, root: Path, run_id: str, save_span: Callable[[dict], None]) -> None:
        """验证运行标识，固定写入追踪根目录下的单个 JSONL 文件。"""
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id) is None:
            raise ValueError("无效的追踪运行标识")
        resolved_root = root.resolve()
        self.path = (resolved_root / f"{run_id}.jsonl").resolve()
        if not self.path.is_relative_to(resolved_root):
            raise ValueError("追踪路径超出授权目录")
        self._save_span = save_span
        self._lock = threading.Lock()

    def __call__(self, span: Span) -> None:
        """独立写入两个本地通道，不保存模型正文或异常消息。"""
        payload = span.model_dump(mode="json")
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
            except (OSError, ValueError):
                LOGGER.warning("追踪 JSONL 写入失败")
            try:
                self._save_span(payload)
            except Exception:  # 存储适配器故障只影响观测；不泄漏数据库异常正文。
                LOGGER.warning("追踪 SQLite 写入失败")
