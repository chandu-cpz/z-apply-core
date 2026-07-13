from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.prompts import load_prompt


def build_authentication_specialist(tools: Sequence[BaseTool]) -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "AuthenticationSpecialist",
            "description": (
                "Resolve one visible login, email verification, OTP, or identity gate in "
                "the current shared browser, then return fresh browser evidence."
            ),
            "system_prompt": load_prompt("authentication_specialist.md"),
            "tools": list(tools),
        },
    )
