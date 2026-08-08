from __future__ import annotations

import unittest

from qdrant_client import QdrantClient

from z_apply_core.memory.applicant_memory import (
    CandidateMemory,
    is_sensitive_fact_label,
    sanitize_candidate_facts,
)


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
        self.assertEqual(result["memory_status"], "exact")
        matches = result["matches"]
        self.assertIsInstance(matches, list)
        self.assertEqual(matches[0]["field_label"], "Expected Salary")
        self.assertEqual(matches[0]["answer"], "18 LPA")
        self.assertEqual(matches[0]["source"], "human_answer")

        await self.memory.remember_human_answer(
            field_label="Expected Salary",
            question="What is your current expected annual salary?",
            answer="20 LPA",
        )
        updated = await self.memory.lookup(
            field_label="Expected Salary",
            question="What compensation do you expect?",
        )
        self.assertEqual(updated["matches"][0]["answer"], "20 LPA")

    async def test_resume_fact_is_stored_and_exact_lookup_resolves_it(self) -> None:
        stored = await self.memory.remember_resume_fact(
            field_label="First name",
            answer="Chandrakanth",
        )

        result = await self.memory.lookup(
            field_label="First Name",
            question="First Name",
        )

        self.assertTrue(stored)
        self.assertEqual(result["memory_status"], "exact")
        matches = result["matches"]
        self.assertEqual(matches[0]["field_label"], "First name")
        self.assertEqual(matches[0]["answer"], "Chandrakanth")
        self.assertEqual(matches[0]["source"], "resume")

    async def test_resume_fact_never_overrides_existing_stored_answer(self) -> None:
        await self.memory.remember_human_answer(
            field_label="Last Name",
            question="What is your last name?",
            answer="Varakala",
        )

        stored = await self.memory.remember_resume_fact(
            field_label="Last Name",
            answer="V",
        )

        self.assertFalse(stored)
        result = await self.memory.lookup(
            field_label="Last Name",
            question="Last Name",
        )
        self.assertEqual(result["memory_status"], "exact")
        self.assertEqual(result["matches"][0]["answer"], "Varakala")
        self.assertEqual(result["matches"][0]["source"], "human_answer")

    async def test_resume_fact_refreshes_changed_resume_copy(self) -> None:
        first = await self.memory.remember_resume_fact(
            field_label="Email",
            answer="old@example.com",
        )
        refreshed = await self.memory.remember_resume_fact(
            field_label="Email",
            answer="new@example.com",
        )

        self.assertTrue(first)
        self.assertTrue(refreshed)
        result = await self.memory.lookup(field_label="Email", question="Email address")
        self.assertEqual(result["memory_status"], "exact")
        self.assertEqual(result["matches"][0]["answer"], "new@example.com")
        self.assertEqual(result["matches"][0]["source"], "resume")

    async def test_resume_fact_seeding_is_idempotent(self) -> None:
        first = await self.memory.remember_resume_fact(
            field_label="Email",
            answer="chandrakanthvarakala@gmail.com",
        )
        second = await self.memory.remember_resume_fact(
            field_label="Email",
            answer="chandrakanthvarakala@gmail.com",
        )

        self.assertTrue(first)
        self.assertFalse(second)
        result = await self.memory.lookup(field_label="Email", question="Email address")
        self.assertEqual(result["memory_status"], "exact")
        self.assertEqual(result["matches"][0]["answer"], "chandrakanthvarakala@gmail.com")

    async def test_all_facts_lists_stored_facts_with_sources(self) -> None:
        await self.memory.remember_human_answer(
            field_label="Expected Salary",
            question="What is your expected annual salary?",
            answer="18 LPA",
        )
        await self.memory.remember_resume_fact(
            field_label="First name",
            answer="Chandrakanth",
        )

        facts = await self.memory.all_facts()

        by_label = {fact["field_label"]: fact for fact in facts}
        self.assertEqual(by_label["Expected Salary"]["answer"], "18 LPA")
        self.assertEqual(by_label["Expected Salary"]["source"], "human_answer")
        self.assertEqual(by_label["First name"]["answer"], "Chandrakanth")
        self.assertEqual(by_label["First name"]["source"], "resume")

    async def test_all_facts_empty_collection_is_well_formed(self) -> None:
        self.assertEqual(await self.memory.all_facts(), [])

    async def test_empty_collection_does_not_claim_candidate_fact(self) -> None:
        result = await self.memory.lookup(
            field_label="Gender",
            question="Select your gender.",
        )

        self.assertEqual(result["memory_status"], "empty")
        self.assertEqual(result["matches"], [])

    async def test_related_field_does_not_leak_job_specific_answer(self) -> None:
        await self.memory.remember_human_answer(
            field_label="Preferred Location",
            question="Choose a preferred location for this application.",
            answer="Pune",
        )

        result = await self.memory.lookup(
            field_label="Location (City)*",
            question="Location (City)*",
        )

        self.assertEqual(result["memory_status"], "no_exact_match")
        self.assertEqual(result["matches"], [])

    async def test_semantic_search_returns_stored_answer(self) -> None:
        await self.memory.remember_human_answer(
            field_label="Preferred Location",
            question="Choose a preferred location for this application.",
            answer="Pune",
        )

        result = await self.memory.search(query="Pune")

        self.assertEqual(result["memory_status"], "semantic")
        matches = result["matches"]
        self.assertIsInstance(matches, list)
        self.assertGreater(len(matches), 0)
        self.assertEqual(matches[0]["answer"], "Pune")

    async def test_semantic_search_is_well_formed_without_close_query(self) -> None:
        await self.memory.remember_human_answer(
            field_label="Preferred Location",
            question="Choose a preferred location for this application.",
            answer="Pune",
        )

        result = await self.memory.search(query="xyzzy")

        self.assertEqual(result["memory_status"], "semantic")
        self.assertIsInstance(result["matches"], list)

    async def test_semantic_search_empty_collection_is_well_formed(self) -> None:
        result = await self.memory.search(query="anything")

        self.assertEqual(result["memory_status"], "empty")
        self.assertEqual(result["matches"], [])


class SensitiveLabelTests(unittest.TestCase):
    def test_matches_credential_labels_only_on_word_boundaries(self) -> None:
        self.assertTrue(is_sensitive_fact_label("Password*"))
        self.assertTrue(is_sensitive_fact_label("Login PIN"))
        self.assertTrue(is_sensitive_fact_label("API key"))
        self.assertFalse(is_sensitive_fact_label("Are you pinned to a location?"))
        self.assertFalse(is_sensitive_fact_label("Are you legally authorized to work?"))
        self.assertFalse(is_sensitive_fact_label("Spinning reserve"))


class SanitizeCandidateFactsTests(unittest.TestCase):
    def test_keeps_useful_facts_and_orders_human_answers_first(self) -> None:
        facts = [
            {"field_label": "Postcode", "answer": "509209", "source": "resume"},
            {"field_label": "Employed at Vena", "answer": "No", "source": "human_answer"},
        ]

        clean = sanitize_candidate_facts(facts, limit=10)

        self.assertEqual(
            [fact["field_label"] for fact in clean],
            ["Employed at Vena", "Postcode"],
        )

    def test_drops_credentials_placeholders_masked_and_self_referential_facts(self) -> None:
        facts = [
            {"field_label": "Postcode", "answer": "509209", "source": "human_answer"},
            {"field_label": "Password*", "answer": "hunter2", "source": "human_answer"},
            {"field_label": "API key", "answer": "sk-1234", "source": "human_answer"},
            {"field_label": "Login PIN", "answer": "1234", "source": "human_answer"},
            {"field_label": "Location", "answer": "Select...", "source": "resume"},
            {"field_label": "Location", "answer": "choose one option", "source": "resume"},
            {"field_label": "Email", "answer": "***masked***", "source": "human_answer"},
            {
                "field_label": "Phone",
                "answer": "\u2022\u2022\u2022\u2022\u2022\u2022",
                "source": "human_answer",
            },
            {"field_label": "Location", "answer": "Location", "source": "resume"},
            {"field_label": "", "answer": "orphan", "source": "resume"},
        ]

        clean = sanitize_candidate_facts(facts, limit=10)

        self.assertEqual([fact["field_label"] for fact in clean], ["Postcode"])

    def test_deduplicates_identical_label_answer_pairs(self) -> None:
        facts = [
            {"field_label": "Postcode", "answer": "509209", "source": "resume"},
            {"field_label": "Postcode", "answer": "509209", "source": "human_answer"},
        ]

        clean = sanitize_candidate_facts(facts, limit=10)

        self.assertEqual(len(clean), 1)

    def test_caps_at_limit(self) -> None:
        facts = [
            {"field_label": f"Fact {index}", "answer": "value", "source": "resume"}
            for index in range(20)
        ]

        self.assertEqual(len(sanitize_candidate_facts(facts, limit=5)), 5)


if __name__ == "__main__":
    unittest.main()
