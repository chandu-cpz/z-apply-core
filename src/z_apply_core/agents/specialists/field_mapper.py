from __future__ import annotations

from typing import cast

from deepagents import SubAgent

from z_apply_core.agents.prompts import load_prompt


def build_field_mapper() -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "FieldMapper",
            "description": "Map visible application fields to candidate information needs.",
            "system_prompt": load_prompt("field_mapper.md"),
            "tools": [],
        },
    )
