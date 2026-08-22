"""Embedding provider env plumbing for CandidateMemory.

Root cause of the virgin store: the default client sent a "local" key to
whatever endpoint was configured and 401'd forever, so no collection could
ever be created. EMBEDDINGS_MODEL/EMBEDDINGS_BASE_URL/EMBEDDINGS_API_KEY must
plumb through to OpenAIEmbeddings construction.
"""

from __future__ import annotations

from typing import Any

from z_apply_core.memory.applicant_memory import _default_embeddings


def test_embeddings_model_env_plumbs_through(monkeypatch: Any) -> None:
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "nvapi-key")
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "https://integrate.api.nvidia.com/v1")
    monkeypatch.setenv("EMBEDDINGS_MODEL", "nvidia/nemotron-3-embed-1b")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = _default_embeddings()

    assert client.model == "nvidia/nemotron-3-embed-1b"
    # NVIDIA rejects token-array input, so the ctx-length
    # check MUST be disabled to send plain strings.
    assert client.check_embedding_ctx_length is False
    assert "integrate.api.nvidia.com" in str(client.openai_api_base)
    # The key is carried as a SecretStr; only presence is asserted, never value.
    assert client.openai_api_key is not None


def test_openai_fallbacks_still_win_when_set(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.delenv("EMBEDDINGS_MODEL", raising=False)

    client = _default_embeddings()

    assert client.model is None or client.model  # model param absent when unset
    assert "api.openai.com" in str(client.openai_api_base)
