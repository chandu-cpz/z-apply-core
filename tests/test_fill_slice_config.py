from __future__ import annotations

import unittest

from z_apply_core.agents.orchestrator import (
    CANDIDATE_CONTEXT_VIRTUAL_PATH,
    DEEPAGENT_FILESYSTEM_PERMISSIONS,
    _task_prompt,
)
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.browser_tools import INITIAL_AGENT_BROWSER_TOOLS
from z_apply_core.cli import DEFAULT_RUN_TASK


class FillSliceConfigTests(unittest.TestCase):
    def test_main_browser_specialist_has_bounded_fill_and_upload_tools(self) -> None:
        self.assertIn("browser_type", INITIAL_AGENT_BROWSER_TOOLS)
        self.assertIn("browser_fill_form", INITIAL_AGENT_BROWSER_TOOLS)
        self.assertIn("browser_select_option", INITIAL_AGENT_BROWSER_TOOLS)
        self.assertIn("browser_file_upload", INITIAL_AGENT_BROWSER_TOOLS)
        self.assertNotIn("browser_navigate", INITIAL_AGENT_BROWSER_TOOLS)

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

    def test_prompts_encode_resume_first_fill_flow(self) -> None:
        orchestrator = load_prompt("orchestrator.md")
        browser = load_prompt("browser_specialist.md")
        answer_writer = load_prompt("answer_writer.md")

        # Resume path must be present
        self.assertIn(".z-apply/input/Chandrakanth-V-Resume.pdf", orchestrator)

        # Write_todos flow check
        self.assertIn("Use `write_todos` for this slice", orchestrator)
        self.assertIn("At most one browser-flow todo should be `in_progress`", orchestrator)

        # One question per AnswerWriter invocation
        self.assertIn("only one field or question per `AnswerWriter` task call", orchestrator)

        # Browser actions are verified by the runtime-owned verifier.
        self.assertIn("runtime automatically runs an independent, read-only verifier", orchestrator)

        # Browser: resume upload and safety
        self.assertIn("This exact filename is `Chandrakanth-V-Resume.pdf`", browser)
        self.assertIn("Do not use `Additional Documents`", browser)
        self.assertIn("do not upload another copy", browser)

        # AnswerWriter: candidate context file
        self.assertIn("/chandrakanth_v_resume.md", answer_writer)
        self.assertIn("exactly one application field or question per invocation", answer_writer)

    def test_default_task_requests_upload_first_without_submit(self) -> None:
        self.assertIn("upload the resume first", DEFAULT_RUN_TASK)
        self.assertIn("do not submit", DEFAULT_RUN_TASK)

    def test_task_prompt_does_not_allow_stopping_after_navigation(self) -> None:
        prompt = _task_prompt(
            job_url="https://example.test/job",
            task=DEFAULT_RUN_TASK,
            snapshot="- snapshot",
        )

        self.assertIn("Do not finish after only navigating to the form.", prompt)
        self.assertIn("attempt the resume upload", prompt)


if __name__ == "__main__":
    unittest.main()
