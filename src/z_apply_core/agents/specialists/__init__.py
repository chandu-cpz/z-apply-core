from __future__ import annotations

from collections.abc import Sequence

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.specialists.answer_writer import build_answer_writer
from z_apply_core.agents.specialists.browser import build_browser_specialist
from z_apply_core.agents.specialists.field_mapper import build_field_mapper
from z_apply_core.agents.specialists.verifier import build_verifier
from z_apply_core.agents.specialists.vision import build_vision_specialist


def build_specialists(browser_tools: Sequence[BaseTool]) -> list[SubAgent]:
    return [
        build_browser_specialist(browser_tools),
        build_vision_specialist(),
        build_field_mapper(),
        build_answer_writer(),
        build_verifier(),
    ]
