"""单次 Agent 运行的跨工作线程与并行工具 span 树。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from ..cancellation import TaskCancelledError

LOGGER = logging.getLogger(__name__)
SpanKind = Literal["run", "plan", "todo", "llm", "tool", "skill", "approval", "verify", "fallback"]
SpanStatus = Literal["ok", "error", "cancelled"]


class Span(BaseModel):
    """只保存计量与关联标识，不保存提示词、参数、输出或异常正文。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    parent_span_id: str | None = None
    trace_id: str
    run_id: str
    kind: SpanKind
    name: str
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_ms: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    cost_estimate: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    status: SpanStatus = "ok"
    error_type: str | None = None


class RunTrace(BaseModel):
    """运行独享的追踪容器；显式传递以兼容生成器切换线程。

    规划与 Todo 保持运行级作用域，并行工具使用独立的上下文父节点。
    结束后拒绝迟到的工作线程更新，避免取消后的成功响应覆盖终态。
    """

    run_id: str
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    spans: list[Span] = Field(default_factory=list)
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)
    _active: list[Span] = PrivateAttr(default_factory=list)
    _starts: dict[str, float] = PrivateAttr(default_factory=dict)
    _exporter: Callable[[Span], None] | None = PrivateAttr(default=None)
    _closed: bool = PrivateAttr(default=False)
    _scope: ContextVar[tuple[str, ...]] = PrivateAttr(
        default_factory=lambda: ContextVar("agent_span_scope", default=()),
    )

    def __init__(self, run_id: str, exporter: Callable[[Span], None] | None = None) -> None:
        """创建根 span，并可选地在每个 span 结束时本地导出。"""
        super().__init__(run_id=run_id)
        self._exporter = exporter
        self.begin("run", "agent_run")

    @property
    def current_span_id(self) -> str | None:
        """优先读取线程继承的调用作用域，再读取跨生成器的规划阶段。"""
        with self._lock:
            for span_id in reversed(self._scope.get()):
                if span_id in self._starts:
                    return span_id
            for span in reversed(self._active):
                if span.kind in {"run", "plan", "todo"}:
                    return span.span_id
            return None

    @contextmanager
    def parent_scope(self, parent_span_id: str | None) -> Iterator[None]:
        """为一个并行执行分支固定父节点，结束后恢复工作线程上下文。"""
        token = self._scope.set((parent_span_id,) if parent_span_id else ())
        try:
            yield
        finally:
            self._scope.reset(token)

    def begin(self, kind: SpanKind, name: str) -> Span | None:
        """在当前活动父节点下开始一个执行片段。"""
        with self._lock:
            if self._closed:
                return None
            span = Span(
                run_id=self.run_id, trace_id=self.trace_id, kind=kind, name=name,
                parent_span_id=self.current_span_id,
            )
            self.spans.append(span)
            self._active.append(span)
            self._starts[span.span_id] = time.monotonic()
            if kind not in {"run", "plan", "todo"}:
                self._scope.set((*self._scope.get(), span.span_id))
            return span

    def end(
        self, span: Span | None, *, status: SpanStatus = "ok",
        error_type: str | None = None, input_tokens: int | None = None,
        output_tokens: int | None = None, cost_estimate: float | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """结束片段并导出；同一片段只结束一次。"""
        if span is None:
            return
        with self._lock:
            started = self._starts.pop(span.span_id, None)
            if started is None:
                return
            span.duration_ms = duration_ms if duration_ms is not None else max(0, round((time.monotonic() - started) * 1000))
            span.status = status
            span.error_type = error_type
            if span.kind in {"run", "plan", "todo", "tool", "skill"}:
                llms = self._descendant_llms(span)
                if llms or span.kind in {"run", "plan", "todo"}:
                    summary = self._summarize(llms)
                    input_tokens = summary["input_tokens"]
                    output_tokens = summary["output_tokens"]
                    cost_estimate = summary["cost_estimate"]
            span.input_tokens = input_tokens
            span.output_tokens = output_tokens
            span.cost_estimate = cost_estimate
            self._active = [item for item in self._active if item.span_id != span.span_id]
            self._scope.set(tuple(item for item in self._scope.get() if item != span.span_id))
            if self._exporter is not None:
                try:
                    self._exporter(span)
                except Exception:  # 观测故障不改变分析行为，也不记录可能含敏感数据的异常正文。
                    LOGGER.warning("运行追踪导出失败", exc_info=False)

    @contextmanager
    def span(self, kind: SpanKind, name: str) -> Iterator[Span | None]:
        """建立可嵌套的片段，并保留异常与取消状态。"""
        span = self.begin(kind, name)
        try:
            yield span
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, (TaskCancelledError, GeneratorExit)) else "error"
            self.end(span, status=status, error_type=type(exc).__name__)
            raise
        else:
            self.end(span)

    def finish(self, status: SpanStatus = "ok", error_type: str | None = None) -> None:
        """关闭根节点与尚未结束的调用，并阻止迟到线程新增片段。"""
        with self._lock:
            self._closed = True
            for span in reversed(list(self._active)):
                self.end(span, status=status, error_type=error_type)

    def _descendant_llms(self, parent: Span) -> list[Span]:
        descendants = {parent.span_id}
        result = []
        for span in self.spans:
            if span.parent_span_id in descendants:
                descendants.add(span.span_id)
                if span.kind == "llm":
                    result.append(span)
        return result

    @staticmethod
    def _summarize(spans: list[Span]) -> dict:
        known_input = sum(span.input_tokens or 0 for span in spans)
        known_output = sum(span.output_tokens or 0 for span in spans)
        missing_input = any(span.input_tokens is None for span in spans)
        missing_output = any(span.output_tokens is None for span in spans)
        missing_cost = any(span.cost_estimate is None for span in spans)
        return {
            "llm_calls": len(spans),
            "unknown_usage_calls": sum(span.input_tokens is None or span.output_tokens is None for span in spans),
            "input_tokens": None if missing_input else known_input,
            "output_tokens": None if missing_output else known_output,
            "known_input_tokens": known_input,
            "known_output_tokens": known_output,
            "usage_status": "unknown" if missing_input or missing_output else "complete",
            "cost_estimate": None if missing_cost else sum(span.cost_estimate or 0 for span in spans),
            "cost_currency": "USD",
        }

    def usage_summary(self) -> dict:
        """汇总真实模型调用，并分别提供 Todo 与工具的归因结果。"""
        with self._lock:
            result = self._summarize([span for span in self.spans if span.kind == "llm"])
            for kind in ("todo", "tool"):
                result[f"by_{kind}"] = [
                    {"span_id": span.span_id, "name": span.name, **self._summarize(self._descendant_llms(span))}
                    for span in self.spans if span.kind == kind
                ]
            return result
