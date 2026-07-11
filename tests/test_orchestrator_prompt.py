from __future__ import annotations

import unittest
from pathlib import Path

ORCHESTRATOR_PROMPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "z_apply_core"
    / "agents"
    / "prompts"
    / "orchestrator.md"
)


class OrchestratorPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = ORCHESTRATOR_PROMPT.read_text(encoding="utf-8")

    def test_verifier_error_verdict_documented(self) -> None:
        self.assertIn("`verifier_error`", self.text)
        self.assertIn("verification infrastructure failed", self.text)

    def test_verified_verdict_documented(self) -> None:
        self.assertIn("`verified`", self.text)
        self.assertIn("browser evidence proves the requested postcondition", self.text)

    def test_not_verified_verdict_documented(self) -> None:
        self.assertIn("`not_verified`", self.text)
        self.assertIn("verifier successfully ran", self.text)

    def test_blocked_verdict_documented(self) -> None:
        self.assertIn("`blocked`", self.text)
        self.assertIn("concrete browser condition", self.text)

    def test_technical_failure_recovery_section_exists(self) -> None:
        self.assertIn("## Technical failure recovery", self.text)

    def test_no_immediate_repeat_rule(self) -> None:
        self.assertIn("Do not immediately repeat the same mutation", self.text)

    def test_fresh_evidence_before_retry(self) -> None:
        self.assertIn("Obtain fresh browser evidence first", self.text)

    def test_max_two_attempts_rule(self) -> None:
        self.assertIn("retry it at most once when safe", self.text)
        self.assertIn("After two failed attempts at the same semantic operation", self.text)

    def test_ask_human_not_for_technical_failures(self) -> None:
        self.assertIn(
            "Never call `ask_human` merely because browser automation", self.text
        )

    def test_verifier_error_not_treated_as_not_verified(self) -> None:
        collapsed = " ".join(self.text.split())
        self.assertIn("Never treat a `verifier_error` as `not_verified`", collapsed)


if __name__ == "__main__":
    unittest.main()
