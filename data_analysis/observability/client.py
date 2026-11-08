"""不改变模型响应和流式片段的客户端装饰器。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from ..cancellation import TaskCancelledError
from .trace import RunTrace
from .usage import TokenPricing, extract_usage


class TracedLlmClient:
    """为 invoke 与 stream 采集计量，保持原客户端的兼容降级行为。"""

    def __init__(
        self, client: Any, trace: RunTrace, *, model_name: str | None = None,
        pricing: TokenPricing | None = None,
    ) -> None:
        """绑定当前运行与可选单价；未配置价格时成本为未知。"""
        self._client = client
        self._trace = trace
        self._model_name = model_name or getattr(client, "model_name", "llm")
        self._pricing = pricing

    def __getattr__(self, name: str) -> Any:
        if name == "stream":
            if callable(getattr(self._client, "stream", None)):
                return self._stream
            raise AttributeError(name)
        return getattr(self._client, name)

    @property
    def disable_streaming(self) -> Any:
        """读取底层流式开关，保留现有空流降级逻辑。"""
        return self._client.disable_streaming

    @disable_streaming.setter
    def disable_streaming(self, value: Any) -> None:
        self._client.disable_streaming = value

    def invoke(self, messages: Sequence[tuple[str, str]], **kwargs: Any) -> Any:
        """原样返回响应，同时保存本次调用的用量、耗时和状态。"""
        span = self._trace.begin("llm", self._model_name)
        try:
            response = self._client.invoke(messages, **kwargs)
        except BaseException as exc:
            self._trace.end(span, status="cancelled" if isinstance(exc, TaskCancelledError) else "error", error_type=type(exc).__name__)
            raise
        input_tokens, output_tokens = extract_usage(response)
        self._trace.end(
            span, input_tokens=input_tokens, output_tokens=output_tokens,
            cost_estimate=self._pricing.estimate(input_tokens, output_tokens) if self._pricing else None,
        )
        return response

    def _stream(self, messages: Sequence[tuple[str, str]], **kwargs: Any) -> Iterator[Any]:
        span = self._trace.begin("llm", self._model_name)
        input_tokens: int | None = None
        output_tokens: int | None = None
        status = "ok"
        error_type = None
        stream = None
        try:
            stream = self._client.stream(messages, **kwargs)
            for chunk in stream:
                chunk_input, chunk_output = extract_usage(chunk)
                # LangChain 的流式 usage 为可累加增量；无正文的计量尾片段也必须消费。
                if chunk_input is not None:
                    input_tokens = (input_tokens or 0) + chunk_input
                if chunk_output is not None:
                    output_tokens = (output_tokens or 0) + chunk_output
                yield chunk
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, (TaskCancelledError, GeneratorExit)) else "error"
            error_type = type(exc).__name__
            raise
        finally:
            self._trace.end(
                span, status=status, error_type=error_type,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cost_estimate=self._pricing.estimate(input_tokens, output_tokens) if self._pricing else None,
            )
            if callable(getattr(stream, "close", None)):
                stream.close()
