from __future__ import annotations

import unittest

from z_apply_core.agents.specialists.answer_writer import build_answer_writer


class AnswerWriterEvidenceTests(unittest.TestCase):
    def test_resume_is_available_without_an_evidence_tool_call(self) -> None:
        resume = "# Candidate\n\nFamily name: V"
        specialist = build_answer_writer(candidate_resume=resume)

        self.assertIn(resume, specialist["system_prompt"])
        self.assertEqual(specialist["tools"], [])


if __name__ == "__main__":
    unittest.main()
