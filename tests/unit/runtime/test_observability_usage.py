"""用量边界与透明模型包装的回归测试。"""

from types import SimpleNamespace

import pytest

from data_analysis.observability.client import TracedLlmClient
from data_analysis.observability.trace import RunTrace
from data_analysis.observability.usage import extract_usage


@pytest.mark.parametrize(
    ("metadata", "fallback", "expected"),
    [
        ({"input_tokens": 12, "output_tokens": 4}, {}, (12, 4)),
        (None, {"token_usage": {"prompt_tokens": 9, "completion_tokens": 3}}, (9, 3)),
        (None, {}, (None, None)),
        ({"input_tokens": -1, "output_tokens": "4"}, {}, (None, None)),
        ({"input_tokens": True}, {"token_usage": {"prompt_tokens": 8}}, (8, None)),
    ],
)
def test_usage_preserves_unknown_and_rejects_malformed_counts(metadata, fallback, expected):
    response = SimpleNamespace(usage_metadata=metadata, response_metadata=fallback)
    assert extract_usage(response) == expected


def test_invoke_returns_original_response_and_tracks_errors():
    response = SimpleNamespace(usage_metadata={"input_tokens": 12, "output_tokens": 4})

    class Client:
        def invoke(self, messages):
            if not messages:
                raise ValueError("敏感信息不应进入追踪")
            return response

    trace = RunTrace("run-test")
    client = TracedLlmClient(Client(), trace, model_name="mock")
    assert client.invoke([("user", "问题")]) is response
    with pytest.raises(ValueError):
        client.invoke([])
    trace.finish()
    spans = [span for span in trace.spans if span.kind == "llm"]
    assert len(spans) == 2
    assert spans[0].input_tokens == 12
    assert spans[1].error_type == "ValueError"
    assert "敏感信息" not in trace.model_dump_json()
    assert trace.usage_summary()["input_tokens"] is None
    assert trace.usage_summary()["known_input_tokens"] == 12


def test_stream_preserves_chunks_including_usage_only_chunk():
    chunks = [
        SimpleNamespace(content="正文", usage_metadata=None),
        SimpleNamespace(content="", usage_metadata={"input_tokens": 10, "output_tokens": 2}),
    ]

    class Client:
        def stream(self, messages):
            yield from chunks

    trace = RunTrace("run-stream")
    client = TracedLlmClient(Client(), trace)
    assert list(client.stream([])) == chunks
    trace.finish()
    spans = [span for span in trace.spans if span.kind == "llm"]
    assert len(spans) == 1
    assert (spans[0].input_tokens, spans[0].output_tokens) == (10, 2)


def test_client_without_stream_keeps_invoke_fallback():
    client = TracedLlmClient(SimpleNamespace(invoke=lambda messages: "原文"), RunTrace("fallback"))
    assert not callable(getattr(client, "stream", None))
    assert client.invoke([]) == "原文"
