from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.prompts import load_prompt


def build_field_mapper(browser_tools: Sequence[BaseTool]) -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "FieldMapper",
            "description": "Map visible application fields to candidate information needs.",
            "system_prompt": load_prompt("field_mapper.md"),
            "tools": list(browser_tools),
        },
    )
