from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.prompts import load_prompt


def build_vision_specialist(browser_tools: Sequence[BaseTool]) -> SubAgent:
    screenshot_tools = [tool for tool in browser_tools if tool.name == "browser_take_screenshot"]
    return cast(
        SubAgent,
        {
            "name": "VisionSpecialist",
            "description": (
                "Capture and interpret the current page when visual evidence is needed "
                "and DOM or ARIA evidence cannot answer a specific question."
            ),
            "system_prompt": load_prompt("vision_specialist.md"),
            "tools": screenshot_tools,
        },
    )
