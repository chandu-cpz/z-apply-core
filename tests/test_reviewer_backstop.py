"""PROP-005 S3: reviewer fabrication backstop (value provenance via lookup)."""

from __future__ import annotations

import unittest

from z_apply_core.agents.prompts import load_prompt


class ReviewerPromptBackstopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prompt = load_prompt("submission_reviewer.md")

    def test_prompt_carries_fabrication_rule(self) -> None:
        self.assertIn("fabrication backstop", self.prompt.lower())
        self.assertIn("FABRICATED", self.prompt)
        self.assertIn("lookup_candidate_memory", self.prompt)

    def test_prompt_names_lookup_before_approval(self) -> None:
        # The flow must force lookups BEFORE the human approval step.
        flow = self.prompt.split("## Flow", 1)[1].split("## Value provenance", 1)[0]
        self.assertIn("value-provenance check", flow)

    def test_prompt_keeps_plausibility_exception_out(self) -> None:
        self.assertIn("no plausibility exception", self.prompt)


if __name__ == "__main__":
    unittest.main()
