from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.prompts import load_prompt


def build_browser_specialist(browser_tools: Sequence[BaseTool]) -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "BrowserSpecialist",
            "description": "Inspect and operate the browser through Playwright MCP tools.",
            "system_prompt": load_prompt("browser_specialist.md"),
            "tools": list(browser_tools),
        },
    )
