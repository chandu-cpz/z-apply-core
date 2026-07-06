from __future__ import annotations

from typing import cast

from deepagents import SubAgent

from z_apply_core.agents.prompts import load_prompt


def build_answer_writer() -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "AnswerWriter",
            "description": "Draft concise answers for application questions.",
            "system_prompt": load_prompt("answer_writer.md"),
            "tools": [],
        },
    )
