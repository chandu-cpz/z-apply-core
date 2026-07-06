from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.prompts import load_prompt


def build_specialists(browser_tools: Sequence[BaseTool]) -> list[SubAgent]:
    specialists = [
        {
            "name": "BrowserSpecialist",
            "description": "Inspect and operate the browser through Playwright MCP tools.",
            "system_prompt": load_prompt("browser_specialist.md"),
            "tools": list(browser_tools),
        },
        {
            "name": "VisionSpecialist",
            "description": (
                "Interpret screenshots and visual layout when DOM evidence is insufficient."
            ),
            "system_prompt": load_prompt("vision_specialist.md"),
            "tools": [],
        },
        {
            "name": "FieldMapper",
            "description": "Map visible application fields to candidate information needs.",
            "system_prompt": load_prompt("field_mapper.md"),
            "tools": [],
        },
        {
            "name": "AnswerWriter",
            "description": "Draft concise answers for application questions.",
            "system_prompt": load_prompt("answer_writer.md"),
            "tools": [],
        },
        {
            "name": "Verifier",
            "description": "Verify whether evidence supports continuing, blocking, or failing.",
            "system_prompt": load_prompt("verifier.md"),
            "tools": [],
        },
    ]
    return cast(list[SubAgent], specialists)
