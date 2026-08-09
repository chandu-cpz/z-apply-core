from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.authentication import AUTHENTICATION_SYSTEM_PROMPT


def build_authentication_specialist(tools: Sequence[BaseTool]) -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "AuthenticationSpecialist",
            "description": (
                "Resolve one visible login, email verification, OTP, or identity gate in "
                "the current shared browser, then return fresh browser evidence."
            ),
            "system_prompt": AUTHENTICATION_SYSTEM_PROMPT,
            "tools": list(tools),
        },
    )
