from __future__ import annotations

import unittest
from pathlib import Path

BROWSER_SPECIALIST_PROMPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "z_apply_core"
    / "agents"
    / "prompts"
    / "browser_specialist.md"
)


class BrowserSpecialistPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = BROWSER_SPECIALIST_PROMPT.read_text(encoding="utf-8")

    def test_snapshot_artifact_section_exists(self) -> None:
        self.assertIn("## Snapshot artifact consumption", self.text)

    def test_do_not_repeat_mutation_on_artifact(self) -> None:
        collapsed = " ".join(self.text.split())
        self.assertIn("Do NOT repeat the mutation", collapsed)

    def test_fresh_snapshot_or_read_file(self) -> None:
        collapsed = " ".join(self.text.split())
        self.assertIn("browser_snapshot", collapsed)
        self.assertIn("read_file", collapsed)

    def test_resume_upload_exception_preserved(self) -> None:
        collapsed = " ".join(self.text.split())
        self.assertIn(
            "after clicking a file control to open the native chooser", collapsed
        )

    def test_success_condition_evidence_required(self) -> None:
        collapsed = " ".join(self.text.split())
        self.assertIn("inspect the post-action evidence", collapsed)
        self.assertIn(
            "Only after inspecting fresh evidence may you claim success or failure",
            collapsed,
        )


if __name__ == "__main__":
    unittest.main()
