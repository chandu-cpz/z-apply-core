from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.prompts import load_prompt

# Read-only evidence tools the Submission Reviewer may use before and after
# the approved click. The submit click itself is the dedicated
# submit_approved_application tool; no other mutation tool is exposed.
REVIEWER_BROWSER_TOOLS = frozenset({"browser_observe", "browser_snapshot", "browser_find"})


def build_submission_reviewer(
    tools: Sequence[BaseTool] = (),
) -> SubAgent:
    """Build the Submission Reviewer specialist.

    The reviewer independently verifies the finished application against fresh
    browser evidence, requests the human's final approval once through the
    approval tool, clicks the submit control through the guarded executor
    after approval, and returns a free-text report beginning with
    ``SUBMITTED:`` or ``REVIEW_FEEDBACK:``. The parent reads that report.
    """
    return cast(
        SubAgent,
        {
            "name": "SubmissionReviewer",
            "description": (
                "Independently verify the finished application against fresh browser "
                "evidence, request the human's final approval once, click the submit "
                "control after approval, and report the outcome. Use only when the "
                "form is complete and ready for final submission."
            ),
            "system_prompt": load_prompt("submission_reviewer.md"),
            "tools": list(tools),
        },
    )
