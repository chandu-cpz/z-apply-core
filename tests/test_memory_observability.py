"""DEC-015 fixes: embedding settings via pydantic Settings + memory.stored events.

1. _default_embeddings reads EMBEDDINGS_* through load_settings (so .env works
   without process-env injection hacks), with OPENAI_* fallbacks preserved.
2. Successful remember_human_answer/remember_resume_fact writes emit a
   persisted memory_stored event — silent 401s can never masquerade as success.
"""

from __future__ import annotations

import asyncio
from typing import Any

from z_apply_core.memory.applicant_memory import CandidateMemory, _default_embeddings
from z_apply_core.stream_events import FrameworkTraceEvent


def test_embeddings_settings_plumb_through(monkeypatch: Any) -> None:
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "nvapi-key")
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "https://integrate.api.nvidia.com/v1")
    monkeypatch.setenv("EMBEDDINGS_MODEL", "nvidia/nemotron-3-embed-1b")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = _default_embeddings()

    assert client.model == "nvidia/nemotron-3-embed-1b"
    assert "integrate.api.nvidia.com" in str(client.openai_api_base)
    assert client.openai_api_key is not None
    # NVIDIA rejects token arrays ("invalid type: sequence"): with an explicit
    # model configured, raw strings must reach the endpoint untokenized.
    assert client.check_embedding_ctx_length is False


def test_openai_fallbacks_still_apply(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.delenv("EMBEDDINGS_MODEL", raising=False)

    client = _default_embeddings()

    assert "api.openai.com" in str(client.openai_api_base)


class _FakeEmbeddings:
    """Deterministic embeddings; no endpoint needed."""

    _dim = 8

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t) % 7)] * self._dim for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text) % 7)] * self._dim


class _Sink:
    def __init__(self) -> None:
        self.events: list[FrameworkTraceEvent] = []

    async def accept(self, event: FrameworkTraceEvent) -> None:
        self.events.append(event)


def _memory_with_sink(sink: _Sink) -> CandidateMemory:
    from qdrant_client import QdrantClient

    return CandidateMemory(
        client=QdrantClient(location=":memory:"),
        embeddings=_FakeEmbeddings(),  # type: ignore[arg-type]
        event_sink=sink,
    )


def test_successful_human_answer_write_emits_memory_stored() -> None:
    sink = _Sink()
    memory = _memory_with_sink(sink)

    stored = asyncio.run(
        memory.remember_human_answer(
            field_label="Expected Salary",
            question="What is your expected compensation?",
            answer="12 LPA",
        )
    )
    asyncio.run(_flush())

    assert stored is True
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.event == "memory_stored"
    assert event.data["source"] == "human_answer"
    assert event.data["field_label"] == "Expected Salary"
    # The answer VALUE must never ride the event; only its size.
    assert event.data["answer_chars"] == len("12 LPA")
    assert "12 LPA" not in str(event.data)


def test_failed_write_emits_nothing(monkeypatch: Any) -> None:
    sink = _Sink()
    memory = _memory_with_sink(sink)

    class _Broken:
        def embed_documents(self, texts):
            raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(memory, "_embeddings", _Broken())
    stored = asyncio.run(memory.remember_human_answer(field_label="X", question="q?", answer="a"))
    asyncio.run(_flush())

    assert stored is False
    assert sink.events == []


async def _flush() -> None:
    await asyncio.sleep(0.05)
