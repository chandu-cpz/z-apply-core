from __future__ import annotations

import asyncio
import atexit
import functools
import logging
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, cast

from qdrant_client import QdrantClient, models

from z_apply_core.paths import qdrant_path
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent
from z_apply_core.text_utils import alnum_key

logger = logging.getLogger(__name__)

MEMORY_PATH = qdrant_path()
MEMORY_COLLECTION = "z_apply_core_applicant_memory_v1"
MEMORY_NAMESPACE = uuid.UUID("f0e95a1d-6811-4fe6-a938-fb1153f3b8a9")
_MEMORY_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="candidate-memory")
atexit.register(_MEMORY_EXECUTOR.shutdown, wait=True, cancel_futures=True)

# Labels that identify credential/secret facts. Such facts are never embedded
# in an agent prompt and never surfaced by the lookup tool: a password or token
# that leaked into memory during an earlier run must not reach any model.
SECRET_LABEL_KEYWORDS: frozenset[str] = frozenset(
    {
        "password",
        "passcode",
        "pass phrase",
        "passphrase",
        "secret",
        "credential",
        "credentials",
        "token",
        "api key",
        "apikey",
        "api_key",
        "access key",
        "private key",
        "authorization",
        "pin",
    }
)

_MASKED_MARKERS = ("***", "redacted", "masked")
_JUNK_ANSWER_VALUES = frozenset(
    {
        "select",
        "select...",
        "select…",
        "choose",
        "choose...",
        "choose…",
        "choose one",
        "choose one option",
        "please select",
        "please choose",
        "-",
        "--",
        "---",
    }
)
_SOURCE_PRIORITY = {"human_answer": 0, "resume": 1}


def is_sensitive_fact_label(label: str) -> bool:
    """True when a stored fact label identifies a credential/secret value."""
    folded = " ".join(label.casefold().split())
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", folded)
        for keyword in SECRET_LABEL_KEYWORDS
    )


def sanitize_candidate_facts(
    facts: list[dict[str, object]],
    *,
    limit: int = 60,
) -> list[dict[str, object]]:
    """Filter stored facts down to prompt-safe, useful candidate evidence.

    Drops empty, credential-labeled, masked, placeholder, and self-referential
    facts, deduplicates by (label, answer), orders explicit human answers
    before resume copies, and caps the list so the embedded prompt section
    stays bounded. The remaining facts are still evidence only; the consumer
    must never treat them as instructions.
    """
    clean: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for fact in facts:
        label = str(fact.get("field_label", "")).strip()
        answer = str(fact.get("answer", "")).strip()
        if not label or not answer:
            continue
        if is_sensitive_fact_label(label):
            continue
        if any(marker in answer.casefold() for marker in _MASKED_MARKERS):
            continue
        if re.fullmatch(r"[*•xX]{3,}", answer):
            continue
        if answer.casefold() in _JUNK_ANSWER_VALUES:
            continue
        if answer.casefold() == label.casefold():
            continue
        dedupe_key = (label.casefold(), answer.casefold())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        clean.append(fact)
    clean.sort(key=lambda fact: _SOURCE_PRIORITY.get(str(fact.get("source", "")), 2))
    return clean[:limit]


class EmbeddingClient(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _default_embeddings() -> EmbeddingClient:
    """Resolve the default OpenAI-compatible embedding client.

    Env contract: EMBEDDINGS_API_KEY/EMBEDDINGS_BASE_URL select the
    provider endpoint; EMBEDDINGS_MODEL selects the embedding model (e.g.
    nvidia/nemotron-3-embed-1b against NVIDIA's OpenAI-compatible build API).
    Settings are read through pydantic ``load_settings`` so the .env file works
    without process-env injection; OPENAI_* variables remain as fallbacks for
    existing local setups. Without a real endpoint the client 401s on first
    use — memory stays empty and every lookup reports unavailable rather than
    fabricating matches.
    """
    from langchain_openai import OpenAIEmbeddings
    from pydantic import SecretStr

    from z_apply_core.config import load_settings

    settings = load_settings()
    api_key = settings.embeddings_api_key or os.environ.get("OPENAI_API_KEY") or "local"
    base_url = settings.embeddings_base_url or os.environ.get("OPENAI_BASE_URL") or ""
    model = settings.embeddings_model
    kwargs: dict[str, Any] = {"api_key": SecretStr(api_key)}
    if base_url:
        kwargs["base_url"] = base_url
    if model:
        kwargs["model"] = model
        # langchain's default path tokenizes input and sends
        # TOKEN ARRAYS; NVIDIA's endpoint rejects sequences ("invalid type:
        # sequence, expected a string"). Disabling the ctx-length check sends
        # plain strings, which NVIDIA accepts. Coordinator-verified live:
        # 2048-dim vectors returned.
        kwargs["check_embedding_ctx_length"] = False
    return cast(EmbeddingClient, OpenAIEmbeddings(**kwargs))


class CandidateMemory:
    """Local semantic memory of facts explicitly supplied by the candidate."""

    def __init__(
        self,
        *,
        client: QdrantClient | None = None,
        embeddings: EmbeddingClient | None = None,
        collection_name: str = MEMORY_COLLECTION,
        event_sink: FrameworkEventSink | None = None,
    ) -> None:
        self._client = client or QdrantClient(path=str(MEMORY_PATH))
        self._embeddings = embeddings or _default_embeddings()
        self._collection_name = collection_name
        self._event_sink = event_sink
        self._lock = asyncio.Lock()
        self._closed = False
        init_options = getattr(self._client, "_init_options", {})
        self._in_memory = (
            isinstance(init_options, dict) and init_options.get("location") == ":memory:"
        )

    async def remember_human_answer(
        self,
        *,
        field_label: str,
        question: str,
        answer: str,
    ) -> bool:
        try:
            async with self._lock:
                await self._run(
                    self._remember_human_answer,
                    field_label=field_label,
                    question=question,
                    answer=answer,
                )
        except Exception as exc:  # noqa: BLE001 - memory must not discard a human answer
            logger.warning("Candidate-memory ingestion failed: %s", exc)
            return False
        self._emit_stored("human_answer", field_label, answer)
        return True

    async def remember_resume_fact(
        self,
        *,
        field_label: str,
        answer: str,
        question: str = "",
    ) -> bool:
        """Store a resume-derived candidate fact, keeping explicit answers.

        An explicit human answer for the same field label always wins and is
        never overwritten. A previous resume copy is refreshed when the resume
        value changed, so a stale seeded fact does not persist after the resume
        file is updated. Returns True when a fact was stored or refreshed,
        False when the stored value was kept unchanged.
        """
        try:
            async with self._lock:
                stored = await self._run(
                    self._remember_resume_fact,
                    field_label=field_label,
                    answer=answer,
                    question=question,
                )
        except Exception as exc:  # noqa: BLE001 - memory must not block a run
            logger.warning("Candidate-memory resume ingestion failed: %s", exc)
            return False
        if stored:
            self._emit_stored("resume", field_label, answer)
        return bool(stored)

    def _emit_stored(self, source: str, field_label: str, answer: str) -> None:
        """Emit a persisted ``memory.stored`` event after a successful write.

        Stage-timing-style observability: successful memory writes were invisible,
        so a silently-failing embedding endpoint looked identical to success.
        The event carries the write outcome without ever carrying the answer
        value itself.
        """
        if self._event_sink is None:
            return
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info("memory.stored source=%s label=%r", source, field_label)
            return
        loop.create_task(
            self._event_sink.accept(
                FrameworkTraceEvent(
                    event="memory_stored",
                    name="CandidateMemory",
                    data={
                        "source": source,
                        "field_label": field_label,
                        "answer_chars": len(answer or ""),
                    },
                    raw={},
                )
            )
        )

    async def lookup(
        self,
        *,
        field_label: str,
        question: str,
        limit: int = 5,
    ) -> dict[str, object]:
        try:
            async with self._lock:
                return cast(
                    dict[str, object],
                    await self._run(
                        self._lookup,
                        field_label=field_label,
                        question=question,
                        limit=limit,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - an unavailable memory is not a candidate fact
            logger.warning("Candidate-memory lookup failed: %s", exc)
            return {
                "memory_status": "unavailable",
                "field_label": field_label,
                "question": question,
                "matches": [],
            }

    async def all_facts(self, *, limit: int = 200) -> list[dict[str, object]]:
        """Return every stored fact as (field_label, question, answer, source).

        Used to embed candidate memory into an agent prompt so facts are always
        visible to the model regardless of tool-calling reliability. Best-effort:
        any failure returns an empty list and never blocks a run.
        """
        try:
            async with self._lock:
                return cast(
                    list[dict[str, object]],
                    await self._run(self._all_facts, limit=limit),
                )
        except Exception as exc:  # noqa: BLE001 - an unavailable memory is not a candidate fact
            logger.warning("Candidate-memory listing failed: %s", exc)
            return []

    async def search(
        self,
        *,
        query: str,
        limit: int = 5,
    ) -> dict[str, object]:
        try:
            async with self._lock:
                return cast(
                    dict[str, object],
                    await self._run(
                        self._search,
                        query=query,
                        limit=limit,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - an unavailable memory is not a candidate fact
            logger.warning("Candidate-memory search failed: %s", exc)
            return {
                "memory_status": "unavailable",
                "query": query,
                "matches": [],
            }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _MEMORY_EXECUTOR.submit(self._client.close).result(timeout=10)

    async def _run(self, function: Any, /, **kwargs: Any) -> Any:
        if self._closed:
            raise RuntimeError("candidate memory is closed")
        if self._in_memory:
            return function(**kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _MEMORY_EXECUTOR,
            functools.partial(function, **kwargs),
        )

    def _remember_human_answer(self, *, field_label: str, question: str, answer: str) -> None:
        document = f"Field: {field_label}\nQuestion: {question}\nAnswer: {answer}"
        vector = self._embeddings.embed_documents([document])[0]
        self._ensure_collection(vector_size=len(vector))
        field_key = alnum_key(field_label)
        self._client.upsert(
            collection_name=self._collection_name,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid5(MEMORY_NAMESPACE, field_key)),
                    vector=vector,
                    payload={
                        "field_label": field_label,
                        "field_key": field_key,
                        "question": question,
                        "answer": answer,
                        "source": "human_answer",
                    },
                )
            ],
            wait=True,
        )

    def _remember_resume_fact(
        self,
        *,
        field_label: str,
        answer: str,
        question: str = "",
    ) -> bool:
        resolved_question = question or f"From the candidate resume: {field_label}"
        existing = self._lookup(field_label=field_label, question=resolved_question, limit=1)
        if existing.get("memory_status") == "exact":
            matches = existing.get("matches")
            if not isinstance(matches, list) or not matches or not isinstance(matches[0], dict):
                return False
            existing_match = matches[0]
            if existing_match.get("source") == "human_answer":
                return False
            if existing_match.get("answer") == answer:
                return False
        document = f"Field: {field_label}\nQuestion: {resolved_question}\nAnswer: {answer}"
        vector = self._embeddings.embed_documents([document])[0]
        self._ensure_collection(vector_size=len(vector))
        self._client.upsert(
            collection_name=self._collection_name,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid5(MEMORY_NAMESPACE, alnum_key(field_label))),
                    vector=vector,
                    payload={
                        "field_label": field_label,
                        "field_key": alnum_key(field_label),
                        "question": resolved_question,
                        "answer": answer,
                        "source": "resume",
                    },
                )
            ],
            wait=True,
        )
        return True

    def _lookup(self, *, field_label: str, question: str, limit: int) -> dict[str, object]:
        if not self._client.collection_exists(self._collection_name):
            return {
                "memory_status": "empty",
                "field_label": field_label,
                "question": question,
                "matches": [],
            }

        exact_points, _ = self._client.scroll(
            collection_name=self._collection_name,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="field_key",
                        match=models.MatchValue(value=alnum_key(field_label)),
                    )
                ]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if exact_points:
            payload = cast(dict[str, Any], exact_points[0].payload or {})
            return {
                "memory_status": "exact",
                "field_label": field_label,
                "question": question,
                "matches": [self._match_from_payload(payload, similarity=1.0)],
            }

        return {
            "memory_status": "no_exact_match",
            "field_label": field_label,
            "question": question,
            "matches": [],
        }

    def _all_facts(self, *, limit: int) -> list[dict[str, object]]:
        if not self._client.collection_exists(self._collection_name):
            return []
        facts: list[dict[str, object]] = []
        offset: Any = None
        # Page the scroll so a collection larger than one batch is never
        # silently truncated in arbitrary point order.
        while len(facts) < limit:
            points, next_offset = self._client.scroll(
                collection_name=self._collection_name,
                limit=min(500, limit - len(facts)),
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = cast(dict[str, Any], point.payload or {})
                facts.append(
                    {
                        "field_label": str(payload.get("field_label", "")),
                        "question": str(payload.get("question", "")),
                        "answer": str(payload.get("answer", "")),
                        "source": str(payload.get("source", "human_answer")),
                    }
                )
            if next_offset is None:
                break
            offset = next_offset
        return facts

    def _search(self, *, query: str, limit: int) -> dict[str, object]:
        if not self._client.collection_exists(self._collection_name):
            return {
                "memory_status": "empty",
                "query": query,
                "matches": [],
            }

        vector = self._embeddings.embed_query(query)
        response = self._client.query_points(
            collection_name=self._collection_name,
            query=vector,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        matches: list[dict[str, object]] = []
        for point in response.points:
            payload = cast(dict[str, Any], point.payload or {})
            matches.append(self._match_from_payload(payload, similarity=point.score))
        return {
            "memory_status": "semantic",
            "query": query,
            "matches": matches,
            "limit": limit,
        }

    @staticmethod
    def _match_from_payload(
        payload: dict[str, Any],
        *,
        similarity: float,
    ) -> dict[str, object]:
        return {
            "field_label": str(payload.get("field_label", "")),
            "question": str(payload.get("question", "")),
            "answer": str(payload.get("answer", "")),
            "source": str(payload.get("source", "human_answer")),
            "similarity": similarity,
        }

    def _ensure_collection(self, *, vector_size: int) -> None:
        if self._client.collection_exists(self._collection_name):
            return
        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        )
