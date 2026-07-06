from __future__ import annotations

from collections.abc import Sequence

from deepagents import SubAgent
from langchain_core.tools import BaseTool

from z_apply_core.agents.specialists.answer_writer import build_answer_writer
from z_apply_core.agents.specialists.browser import build_browser_specialist
from z_apply_core.agents.specialists.field_mapper import build_field_mapper
from z_apply_core.agents.specialists.verifier import build_verifier
from z_apply_core.agents.specialists.vision import build_vision_specialist
from z_apply_core.browser_tools import VERIFIER_BROWSER_TOOLS


def build_specialists(browser_tools: Sequence[BaseTool]) -> list[SubAgent]:
    read_only_browser_tools = [
        tool for tool in browser_tools if tool.name in VERIFIER_BROWSER_TOOLS
    ]
    return [
        build_browser_specialist(browser_tools),
        build_vision_specialist(),
        build_field_mapper(),
        build_answer_writer(),
        build_verifier(read_only_browser_tools),
    ]
