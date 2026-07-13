from __future__ import annotations

import unittest

from z_apply_core.agents.orchestrator import (
    CANDIDATE_CONTEXT_VIRTUAL_PATH,
    DEEPAGENT_FILESYSTEM_PERMISSIONS,
)
from z_apply_core.browser_tools import INITIAL_AGENT_BROWSER_TOOLS


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

        self.assertIn("/.z-apply/runs", allowed_paths)
        self.assertIn("/.z-apply/runs/**", allowed_paths)
        self.assertIn(CANDIDATE_CONTEXT_VIRTUAL_PATH, allowed_paths)
        self.assertEqual(CANDIDATE_CONTEXT_VIRTUAL_PATH, "/chandrakanth_v_resume.md")

if __name__ == "__main__":
    unittest.main()
