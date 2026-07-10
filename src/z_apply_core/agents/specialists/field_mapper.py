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
            "description": (
                "Read the current application evidence, map every visible field and "
                "challenge, and identify safe fill batches and missing information "
                "without changing browser state."
            ),
            "system_prompt": load_prompt("field_mapper.md"),
            "tools": list(browser_tools),
        },
    )
