from __future__ import annotations

import asyncio
import atexit
import functools
import logging
import re
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol, cast

from langchain_core.tools import BaseTool, tool
from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings
from qdrant_client import QdrantClient, models

logger = logging.getLogger(__name__)

CORE_ROOT = Path(__file__).resolve().parents[3]
MEMORY_PATH = CORE_ROOT / ".z-apply" / "qdrant"
MEMORY_COLLECTION = "z_apply_core_applicant_memory_v1"
MEMORY_NAMESPACE = uuid.UUID("f0e95a1d-6811-4fe6-a938-fb1153f3b8a9")
_MEMORY_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="candidate-memory")
atexit.register(_MEMORY_EXECUTOR.shutdown, wait=True, cancel_futures=True)


def _field_key(field_label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", field_label.casefold())


class EmbeddingClient(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class CandidateMemory:
    """Local semantic memory of facts explicitly supplied by the candidate."""

    def __init__(
        self,
        *,
        client: QdrantClient | None = None,
        embeddings: EmbeddingClient | None = None,
        collection_name: str = MEMORY_COLLECTION,
    ) -> None:
        self._client = client or QdrantClient(path=str(MEMORY_PATH))
        self._embeddings = embeddings or cast(EmbeddingClient, NVIDIAEmbeddings())
        self._collection_name = collection_name
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
        return True

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

    def answer_writer_tools(self) -> list[BaseTool]:
        memory = self

        @tool
        async def lookup_candidate_memory(
            field_label: str,
            question: str,
            limit: int = 5,
        ) -> dict[str, object]:
            """Retrieve explicit candidate facts for one application field.

            Only an exact normalized field-label match is returned as candidate
            evidence. A no_exact_match result contains no usable value; consult the
            resume or human instead. Call for exactly one field and pass its current
            label and question without paraphrasing either.
            """
            return await memory.lookup(
                field_label=field_label,
                question=question,
                limit=limit,
            )

        return [lookup_candidate_memory]

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
        field_key = _field_key(field_label)
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
                        match=models.MatchValue(value=_field_key(field_label)),
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


def build_answer_writer_memory_tools(memory: CandidateMemory | None) -> Sequence[BaseTool]:
    return memory.answer_writer_tools() if memory is not None else ()
