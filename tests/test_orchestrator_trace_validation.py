from __future__ import annotations

import unittest

from z_apply_core.agents.orchestrator import _validated_summary


class OrchestratorTraceValidationTests(unittest.TestCase):
    def test_rejects_resume_upload_claim_without_upload_tool(self) -> None:
        summary = "The resume file has been successfully uploaded."

        result = _validated_summary(summary, [])

        self.assertIn("not_verified", result)
        self.assertIn("without a browser_file_upload call", result)

    def test_rejects_field_mapping_claim_without_field_mapper_task(self) -> None:
        summary = "Field Mapping Result: Phone Number is required."

        result = _validated_summary(summary, [])

        self.assertIn("not_verified", result)
        self.assertIn("without a FieldMapper task call", result)

    def test_rejects_human_question_without_ask_human_tool(self) -> None:
        summary = "Could you provide the phone number for this application?"

        result = _validated_summary(summary, [])

        self.assertIn("not_verified", result)
        self.assertIn("without an ask_human tool call", result)

    def test_rejects_consecutive_browser_actions_before_verifier(self) -> None:
        trace = [
            {
                "source": "BrowserSpecialist",
                "tool_name": "browser_click",
                "input": {"target": "e112"},
            },
            {
                "source": "BrowserSpecialist",
                "tool_name": "browser_click",
                "input": {"target": "e112"},
            },
            {
                "source": "orchestrator",
                "tool_name": "task",
                "input": {"subagent_type": "Verifier"},
            },
        ]

        result = _validated_summary("Form is open.", trace)

        self.assertIn("not_verified", result)
        self.assertIn("before Verifier checked browser_click", result)

    def test_accepts_verified_upload_claim_with_upload_and_verifier(self) -> None:
        trace = [
            {
                "source": "BrowserSpecialist",
                "tool_name": "browser_file_upload",
                "input": {"paths": [".z-apply/input/Chandrakanth-V-Resume.pdf"]},
            },
            {
                "source": "orchestrator",
                "tool_name": "task",
                "input": {"subagent_type": "Verifier"},
            },
        ]
        summary = "The resume file has been successfully uploaded."

        result = _validated_summary(summary, trace)

        self.assertEqual(result, summary)


if __name__ == "__main__":
    unittest.main()
