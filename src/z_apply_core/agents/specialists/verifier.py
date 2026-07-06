from __future__ import annotations

from typing import cast

from deepagents import SubAgent

from z_apply_core.agents.prompts import load_prompt


def build_verifier() -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "Verifier",
            "description": "Verify whether evidence supports continuing, blocking, or failing.",
            "system_prompt": load_prompt("verifier.md"),
            "tools": [],
        },
    )
