from __future__ import annotations

import unittest

from z_apply_core.agents.specialists.answer_writer import build_answer_writer


class AnswerWriterEvidenceTests(unittest.TestCase):
    def test_resume_is_available_without_an_evidence_tool_call(self) -> None:
        resume = "# Candidate\n\nFamily name: V"
        specialist = build_answer_writer(candidate_resume=resume)

        self.assertIn(resume, specialist["system_prompt"])
        self.assertEqual(specialist["tools"], [])

    def test_stored_facts_are_embedded_in_the_system_prompt(self) -> None:
        facts = [
            {"field_label": "Postcode", "answer": "509209", "source": "human_answer"},
            {
                "field_label": "Are you a current employee at Vena Solutions?*",
                "answer": "No",
                "source": "human_answer",
            },
        ]

        specialist = build_answer_writer(candidate_facts=facts)
        prompt = specialist["system_prompt"]

        self.assertIn("## Stored candidate facts", prompt)
        self.assertIn("Postcode: 509209", prompt)
        self.assertIn("Are you a current employee at Vena Solutions?*: No", prompt)

    def test_secret_facts_are_never_embedded(self) -> None:
        facts = [
            {"field_label": "Postcode", "answer": "509209", "source": "human_answer"},
            {"field_label": "Password*", "answer": "hunter2", "source": "human_answer"},
        ]

        specialist = build_answer_writer(candidate_facts=facts)
        prompt = specialist["system_prompt"]

        self.assertIn("Postcode: 509209", prompt)
        self.assertNotIn("hunter2", prompt)

    def test_missing_facts_render_a_placeholder_section(self) -> None:
        specialist = build_answer_writer()

        self.assertIn("## Stored candidate facts", specialist["system_prompt"])
        self.assertIn("(No stored candidate facts are available.)", specialist["system_prompt"])


if __name__ == "__main__":
    unittest.main()
