from __future__ import annotations

import unittest

from qdrant_client import QdrantClient

from z_apply_core.memory.applicant_memory import CandidateMemory
from z_apply_core.memory.tools import make_candidate_memory_tools


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


class CandidateMemoryToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.memory = CandidateMemory(
            client=QdrantClient(":memory:"),
            embeddings=FakeEmbeddings(),
            collection_name="candidate_memory_tools_test",
        )

    async def asyncTearDown(self) -> None:
        self.memory.close()

    async def test_tool_returns_stored_answer_for_exact_label(self) -> None:
        await self.memory.remember_human_answer(
            field_label="Preferred Location",
            question="Choose a preferred location for this application.",
            answer="Pune",
        )

        tools = make_candidate_memory_tools(self.memory)
        tool = next(item for item in tools if item.name == "lookup_candidate_memory")

        result = await tool.ainvoke(
            {"query": "location", "field_label": "Preferred Location"}
        )

        self.assertEqual(result["lookup"]["memory_status"], "exact")
        sources = result["sources"]
        self.assertIsInstance(sources, list)
        self.assertTrue(any(source["answer"] == "Pune" for source in sources))

    async def test_tool_searches_free_text_without_label(self) -> None:
        await self.memory.remember_human_answer(
            field_label="Preferred Location",
            question="Choose a preferred location for this application.",
            answer="Pune",
        )

        tools = make_candidate_memory_tools(self.memory)
        tool = next(item for item in tools if item.name == "lookup_candidate_memory")

        result = await tool.ainvoke({"query": "Pune"})

        self.assertIsNone(result["lookup"])
        self.assertEqual(result["search"]["memory_status"], "semantic")
        sources = result["sources"]
        self.assertIsInstance(sources, list)
        self.assertTrue(any(source["answer"] == "Pune" for source in sources))

    async def test_tool_returns_readable_empty_result(self) -> None:
        tools = make_candidate_memory_tools(self.memory)
        tool = next(item for item in tools if item.name == "lookup_candidate_memory")

        result = await tool.ainvoke({"query": "anything"})

        self.assertEqual(result["lookup"], None)
        self.assertEqual(result["search"]["memory_status"], "empty")
        self.assertEqual(result["sources"], [])


if __name__ == "__main__":
    unittest.main()
