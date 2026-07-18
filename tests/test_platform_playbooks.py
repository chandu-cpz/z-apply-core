from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from langchain_core.tools import ToolException

from z_apply_core.browser_observation import ActionReceipt, BrowserObservation
from z_apply_core.memory.platform_playbooks import PlatformPlaybooks, site_family


class PlatformPlaybookTests(unittest.TestCase):
    def test_site_family_shares_lessons_across_workday_tenants(self) -> None:
        self.assertEqual(
            site_family("https://comcast.wd5.myworkdayjobs.com/apply"),
            "myworkdayjobs.com",
        )

    def test_verified_changed_action_is_persisted_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            playbooks = PlatformPlaybooks(Path(directory))
            receipt = ActionReceipt(
                tool="browser_click_upload",
                arguments={"target": "e10", "paths": ["/private/resume.pdf"]},
                before_revision=4,
                after=BrowserObservation.create(
                    revision=5,
                    url="https://tenant.myworkdayjobs.com/apply",
                    title="Apply",
                    evidence="resume attached",
                ),
                changed=True,
            )

            first = playbooks.remember(
                job_url="https://other.myworkdayjobs.com/job/1",
                situation="A required resume upload is visible.",
                action="Use the atomic upload action on the upload control.",
                expected_outcome="The attached file is visible on the employer form.",
                recovery="Inspect fresh evidence and retry the current upload control.",
                receipt=receipt,
            )
            second = playbooks.remember(
                job_url="https://other.myworkdayjobs.com/job/2",
                situation="A required resume upload is visible.",
                action="Use the atomic upload action on the upload control.",
                expected_outcome="The attached file is visible on the employer form.",
                recovery="Inspect fresh evidence and retry the current upload control.",
                receipt=receipt,
            )

            rendered = playbooks.read_for_url("https://third.myworkdayjobs.com/job/3")
            self.assertIn("Stored one evidence-backed procedure", first)
            self.assertIn("Added independent browser evidence", second)
            self.assertEqual(rendered.count("- Situation:"), 1)
            self.assertEqual(rendered.count("- Browser evidence:"), 2)
            self.assertNotIn("e10", rendered)
            self.assertNotIn("resume.pdf", rendered)

    def test_unchanged_action_cannot_be_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            playbooks = PlatformPlaybooks(Path(directory))
            receipt = ActionReceipt(
                tool="browser_click",
                arguments={},
                before_revision=2,
                after=BrowserObservation.create(
                    revision=2,
                    url="https://example.com/apply",
                    title="Apply",
                    evidence="unchanged",
                ),
                changed=False,
            )

            with self.assertRaisesRegex(ToolException, "observed state change"):
                playbooks.remember(
                    job_url="https://example.com/job",
                    situation="A consent control is visible.",
                    action="Activate the consent control.",
                    expected_outcome="The control is selected.",
                    recovery="Inspect fresh evidence.",
                    receipt=receipt,
                )


if __name__ == "__main__":
    unittest.main()
