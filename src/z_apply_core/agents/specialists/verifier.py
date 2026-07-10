from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.prompts import load_prompt


def build_verifier(
    browser_tools: Sequence[BaseTool],
    *,
    prompt_name: str = "verifier.md",
) -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "Verifier",
            "description": (
                "Independently inspect read-only browser evidence for one named postcondition "
                "and distinguish verified, operation-specific blocked, and not verified."
            ),
            "system_prompt": load_prompt(prompt_name),
            "tools": list(browser_tools),
        },
    )
