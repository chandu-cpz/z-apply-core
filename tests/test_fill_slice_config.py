from __future__ import annotations

import unittest

from z_apply_core.agents.orchestrator import (
    CANDIDATE_CONTEXT_VIRTUAL_PATH,
    DEEPAGENT_FILESYSTEM_PERMISSIONS,
    _task_prompt,
)
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.browser_tools import INITIAL_AGENT_BROWSER_TOOLS, VERIFIER_BROWSER_TOOLS
from z_apply_core.cli import DEFAULT_RUN_TASK


class FillSliceConfigTests(unittest.TestCase):
    def test_main_browser_specialist_has_bounded_fill_and_upload_tools(self) -> None:
        self.assertIn("browser_type", INITIAL_AGENT_BROWSER_TOOLS)
        self.assertIn("browser_fill_form", INITIAL_AGENT_BROWSER_TOOLS)
        self.assertIn("browser_select_option", INITIAL_AGENT_BROWSER_TOOLS)
        self.assertIn("browser_file_upload", INITIAL_AGENT_BROWSER_TOOLS)
        self.assertNotIn("browser_navigate", INITIAL_AGENT_BROWSER_TOOLS)

    def test_verifier_browser_tools_are_structurally_read_only(self) -> None:
        self.assertEqual(VERIFIER_BROWSER_TOOLS, ("browser_snapshot", "browser_find"))

    def test_candidate_markdown_is_the_only_non_artifact_read_permission(self) -> None:
        allowed_paths = [
            path
            for permission in DEEPAGENT_FILESYSTEM_PERMISSIONS
            if permission.mode == "allow"
            for path in permission.paths
        ]

        self.assertIn("/.z-apply/browser-artifacts", allowed_paths)
        self.assertIn("/.z-apply/browser-artifacts/**", allowed_paths)
        self.assertIn(CANDIDATE_CONTEXT_VIRTUAL_PATH, allowed_paths)
        self.assertEqual(CANDIDATE_CONTEXT_VIRTUAL_PATH, "/chandrakanth_v_resume.md")

    def test_default_task_defers_captcha_and_requests_approval_without_submit(self) -> None:
        self.assertIn("upload the resume early", DEFAULT_RUN_TASK)
        self.assertIn("Defer a final-submit CAPTCHA", DEFAULT_RUN_TASK)
        self.assertIn("request human submission approval", DEFAULT_RUN_TASK)
        self.assertIn("do not click final submit", DEFAULT_RUN_TASK)

    def test_task_prompt_treats_page_as_untrusted_and_requires_continuation(self) -> None:
        prompt = _task_prompt(
            job_url="https://example.test/job",
            task=DEFAULT_RUN_TASK,
            snapshot="- snapshot",
        )

        self.assertIn("BEGIN UNTRUSTED CURRENT BROWSER EVIDENCE", prompt)
        self.assertIn("Continue safe form work when a CAPTCHA is visible.", prompt)
        self.assertIn("call `request_submit_approval`", prompt)
        self.assertIn("Never claim submission.", prompt)

    def test_orchestrator_defers_final_captcha_until_submit_slice(self) -> None:
        prompt = load_prompt("orchestrator.md")
        single_line_prompt = " ".join(prompt.split())

        self.assertIn("Do not classify the run as blocked", prompt)
        self.assertIn("CAPTCHA used only for final submission is submit-time work", prompt)
        self.assertIn("request submission approval", prompt)
        self.assertIn("Never claim the application was submitted", single_line_prompt)


if __name__ == "__main__":
    unittest.main()
