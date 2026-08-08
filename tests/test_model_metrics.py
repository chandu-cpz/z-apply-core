from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from z_apply_core.context.model_metrics import (
    CallContent,
    CallMetrics,
    MetricStream,
    content_from_messages,
    extract_cache_read,
    extract_usage_tokens,
    format_call_metrics,
    is_async_iterable,
)


def test_extract_usage_tokens_reads_chatopenai_keys() -> None:
    response = ModelResponse(
        result=[
            SimpleNamespace(
                usage_metadata={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            )
        ]
    )
    assert extract_usage_tokens(response) == (100, 50)


def test_extract_usage_tokens_reads_chatnvidia_keys() -> None:
    response = ModelResponse(
        result=[
            AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 77, "output_tokens": 33, "total_tokens": 110},
            )
        ]
    )
    assert extract_usage_tokens(response) == (77, 33)


def test_extract_usage_tokens_reads_response_metadata_usage() -> None:
    response = ModelResponse(
        result=[
            AIMessage(
                content="ok",
                response_metadata={"usage": {"prompt_tokens": 9, "completion_tokens": 3}},
            )
        ]
    )
    assert extract_usage_tokens(response) == (9, 3)


def test_extract_usage_tokens_stream_usage_takes_priority() -> None:
    response = ModelResponse(
        result=[
            AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            )
        ]
    )
    stream_usage = {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}
    assert extract_usage_tokens(response, stream_usage) == (5, 7)


def test_extract_usage_tokens_returns_none_without_usage() -> None:
    assert extract_usage_tokens(ModelResponse(result=[AIMessage(content="ok")])) is None
    assert extract_usage_tokens(None) is None
    garbage = SimpleNamespace(usage_metadata={"total_tokens": 5})
    assert extract_usage_tokens(ModelResponse(result=[garbage])) is None


def test_is_async_iterable_detects_generators() -> None:
    async def stream():
        yield "chunk"

    assert is_async_iterable(stream())
    assert not is_async_iterable(["chunk"])


@pytest.mark.asyncio
async def test_metric_stream_reports_ttft_and_final_usage() -> None:
    done: list[tuple[CallMetrics, object]] = []

    async def stream():
        yield "first"
        yield "second"
        yield SimpleNamespace(
            usage_metadata={"input_tokens": 77, "output_tokens": 33, "total_tokens": 110}
        )

    wrapped = MetricStream(
        stream(),
        started=time.monotonic(),
        on_done=lambda metrics, content: done.append((metrics, content)),
    )
    items = [item async for item in wrapped]
    assert items == [
        "first",
        "second",
        SimpleNamespace(
            usage_metadata={"input_tokens": 77, "output_tokens": 33, "total_tokens": 110}
        ),
    ]

    assert len(done) == 1
    metrics, content = done[0]
    assert metrics.input_tokens == 77
    assert metrics.output_tokens == 33
    assert metrics.ttft_ms is not None and metrics.ttft_ms >= 0
    assert metrics.duration_ms >= 0
    assert isinstance(content, CallContent)


@pytest.mark.asyncio
async def test_metric_stream_captures_streamed_content() -> None:
    done: list[tuple[CallMetrics, CallContent]] = []

    async def stream():
        yield SimpleNamespace(content="Hello ", reasoning_content="planning ")
        yield SimpleNamespace(
            content=[
                {"type": "thinking", "text": "more thinking"},
                {"type": "text", "text": "world"},
            ]
        )
        yield SimpleNamespace(content="", tool_call_chunks=[])

    wrapped = MetricStream(
        stream(),
        started=time.monotonic(),
        on_done=lambda metrics, content: done.append((metrics, content)),
    )
    _ = [item async for item in wrapped]
    _metrics, content = done[0]
    assert content.text == "Hello world"
    assert content.reasoning == "planning more thinking"


@pytest.mark.asyncio
async def test_metric_stream_captures_streamed_tool_call_chunks() -> None:
    done: list[tuple[CallMetrics, CallContent]] = []

    async def stream():
        yield SimpleNamespace(
            tool_call_chunks=[
                SimpleNamespace(index=0, name="browser_click", id="call_1", args='{"target":'),
                SimpleNamespace(index=0, name="browser_click", id="call_1", args=' "e208"}'),
            ]
        )

    wrapped = MetricStream(
        stream(),
        started=time.monotonic(),
        on_done=lambda metrics, content: done.append((metrics, content)),
    )
    _ = [item async for item in wrapped]
    _metrics, content = done[0]
    assert len(content.tool_calls) == 1
    assert content.tool_calls[0]["name"] == "browser_click"
    assert content.tool_calls[0]["args"] == '{"target": "e208"}'


def test_content_from_messages_extracts_final_response() -> None:
    messages = [
        AIMessage(
            content="final answer",
            additional_kwargs={"reasoning_content": "secret trace"},
            tool_calls=[
                {"name": "browser_fill", "args": {"target": "e1", "value": "x"}, "id": "c1"}
            ],
        )
    ]
    content = content_from_messages(messages)
    assert content.text == "final answer"
    assert content.reasoning == "secret trace"
    assert content.tool_calls[0]["name"] == "browser_fill"


@pytest.mark.asyncio
async def test_metric_stream_on_done_failure_does_not_break_consumption() -> None:
    def boom(_metrics: CallMetrics, _content: CallContent) -> None:
        raise RuntimeError("on_done exploded")

    async def stream():
        yield "only"

    wrapped = MetricStream(stream(), started=0.0, on_done=boom)
    assert [item async for item in wrapped] == ["only"]


def test_call_metrics_tok_per_second_uses_generation_time_when_ttft_known() -> None:
    streamed = CallMetrics(output_tokens=90, duration_ms=3000, ttft_ms=1000)
    assert streamed.tok_per_second == pytest.approx(45.0)
    non_streamed = CallMetrics(output_tokens=90, duration_ms=3000, ttft_ms=None)
    assert non_streamed.tok_per_second == pytest.approx(30.0)
    assert CallMetrics(output_tokens=0, duration_ms=3000).tok_per_second is None


def test_format_call_metrics_renders_values_and_na() -> None:
    metrics = CallMetrics(input_tokens=123, output_tokens=45, duration_ms=2000, ttft_ms=400)
    rendered = format_call_metrics(metrics)
    assert rendered == "in=123 out=45 cache=n/a ttft=400ms tok/s=28.1"
    cached = CallMetrics(
        input_tokens=123, output_tokens=45, cache_read_tokens=111, duration_ms=2000, ttft_ms=400
    )
    assert format_call_metrics(cached) == "in=123 out=45 cache=111 ttft=400ms tok/s=28.1"
    assert format_call_metrics(CallMetrics()) == "in=n/a out=n/a cache=n/a ttft=n/a tok/s=n/a"


def test_extract_usage_tokens_accepts_garbage_result() -> None:
    assert (
        extract_usage_tokens(
            SimpleNamespace(usage_metadata={"input_tokens": "x", "output_tokens": 1})
        )
        is None
    )
    assert extract_usage_tokens("not a response") is None


def test_extract_cache_read_reads_langchain_and_deepseek_keys() -> None:
    assert extract_cache_read({"input_token_details": {"cache_read": 1280}}) == 1280
    assert extract_cache_read({"prompt_tokens_details": {"cached_tokens": 768}}) == 768
    assert (
        extract_cache_read({"prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}) == 900
    )
    assert extract_cache_read({"input_tokens": 1411}) is None
    assert extract_cache_read(None) is None
    assert extract_cache_read({"prompt_tokens_details": {"cached_tokens": "x"}}) is None
