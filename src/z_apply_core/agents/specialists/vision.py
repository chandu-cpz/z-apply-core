from __future__ import annotations

from typing import cast

from deepagents import SubAgent

from z_apply_core.agents.prompts import load_prompt


def build_vision_specialist() -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "VisionSpecialist",
            "description": (
                "Interpret screenshots and visual layout when DOM evidence is insufficient."
            ),
            "system_prompt": load_prompt("vision_specialist.md"),
            "tools": [],
        },
    )
