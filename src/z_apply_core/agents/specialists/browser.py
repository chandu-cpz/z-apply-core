from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.prompts import load_prompt


def build_browser_specialist(
    browser_tools: Sequence[BaseTool],
    *,
    prompt_name: str = "browser_specialist.md",
) -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "BrowserSpecialist",
            "description": (
                "Execute one parent-defined semantic browser operation using as many "
                "Playwright calls as needed, without choosing the application flow or "
                "clicking final submit."
            ),
            "system_prompt": load_prompt(prompt_name),
            "tools": list(browser_tools),
        },
    )
