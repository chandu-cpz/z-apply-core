from __future__ import annotations

import unittest

from qdrant_client import QdrantClient

from z_apply_core.memory.applicant_memory import CandidateMemory


class FakeEmbeddings:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    @staticmethod
    def _embed(text: str) -> list[float]:
        lowered = text.lower()
        return [
            float("salary" in lowered or "compensation" in lowered),
            float("gender" in lowered),
            1.0,
        ]


class CandidateMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.memory = CandidateMemory(
            client=QdrantClient(":memory:"),
            embeddings=FakeEmbeddings(),
            collection_name="candidate_memory_test",
        )

    async def asyncTearDown(self) -> None:
        self.memory.close()

    async def test_returns_agent_readable_explicit_human_fact(self) -> None:
        stored = await self.memory.remember_human_answer(
            field_label="Expected Salary",
            question="What is your expected annual salary?",
            answer="18 LPA",
        )

        result = await self.memory.lookup(
            field_label="Expected Salary",
            question="What is your expected compensation?",
        )

        self.assertTrue(stored)
        self.assertEqual(result["memory_status"], "ready")
        matches = result["matches"]
        self.assertIsInstance(matches, list)
        self.assertEqual(matches[0]["field_label"], "Expected Salary")
        self.assertEqual(matches[0]["answer"], "18 LPA")
        self.assertEqual(matches[0]["source"], "human_answer")

    async def test_empty_collection_does_not_claim_candidate_fact(self) -> None:
        result = await self.memory.lookup(
            field_label="Gender",
            question="Select your gender.",
        )

        self.assertEqual(result["memory_status"], "empty")
        self.assertEqual(result["matches"], [])


if __name__ == "__main__":
    unittest.main()
