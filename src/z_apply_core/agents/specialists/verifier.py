from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.prompts import load_prompt


def build_verifier(browser_tools: Sequence[BaseTool]) -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "Verifier",
            "description": "Verify whether evidence supports continuing, blocking, or failing.",
            "system_prompt": load_prompt("verifier.md"),
            "tools": list(browser_tools),
        },
    )
