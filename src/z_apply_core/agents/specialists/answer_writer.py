from __future__ import annotations

from typing import cast

from deepagents import SubAgent

from z_apply_core.agents.prompts import load_prompt


def build_answer_writer() -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "AnswerWriter",
            "description": (
                "Resolve exactly one application field from explicit candidate, saved-profile, "
                "or prior-human evidence, or report the precise missing human fact."
            ),
            "system_prompt": load_prompt("answer_writer.md"),
            "tools": [],
        },
    )
