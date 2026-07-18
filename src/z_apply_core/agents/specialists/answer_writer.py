from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

from deepagents import SubAgent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, Field

from z_apply_core.agents.prompts import load_prompt

CANDIDATE_FIELD_TOOL_NAME = "resolve_candidate_field"


class CandidateFieldAnswer(BaseModel):
    """One evidence-backed browser-bound candidate resolution."""

    source: Literal["memory", "resume", "human"]
    field_label: str = Field(min_length=1, description="Exact current field label")
    target: str = Field(
        pattern=r"^e\d+$", description="Exact current browser target ref from the task"
    )
    value: str = Field(min_length=1, description="Exact evidence-backed field value")


class CandidateFieldRequest(BaseModel):
    """One candidate field bound to the evidence revision that exposed it."""

    browser_revision: int = Field(ge=1, description="Current browser revision from runtime context")
    field_label: str = Field(
        min_length=1, description="Exact visible label or question for this field"
    )
    target: str = Field(pattern=r"^e\d+$", description="Exact current browser target ref")
    current_value: str = Field(
        description="Exact value currently visible in the target; empty when unresolved"
    )
    control_type: Literal["textbox", "checkbox", "radio", "combobox", "slider"] = Field(
        description="Exact Playwright form control type from current browser evidence"
    )


def make_candidate_field_tool() -> BaseTool:
    """Expose typed candidate delegation; middleware replaces valid calls."""

    @tool(CANDIDATE_FIELD_TOOL_NAME, args_schema=CandidateFieldRequest)
    def resolve_candidate_field(
        browser_revision: int,
        field_label: str,
        target: str,
        current_value: str,
        control_type: str,
    ) -> str:
        """Resolve exactly one candidate field through AnswerWriter.

        Copy only current browser evidence into this request. Do not propose an
        answer. The runtime rejects stale or disabled requests and supplies
        browser-owned choice options without model transcription.
        """
        del (
            browser_revision,
            field_label,
            target,
            current_value,
            control_type,
        )
        raise ToolException("Candidate delegation was not normalized by the runtime.")

    return resolve_candidate_field


def build_answer_writer(
    tools: Sequence[BaseTool] = (),
    *,
    candidate_resume: str = "",
) -> SubAgent:
    resume_evidence = candidate_resume.strip() or "(No prepared resume evidence is available.)"
    return cast(
        SubAgent,
        {
            "name": "AnswerWriter",
            "description": (
                "Resolve exactly one application field from explicit candidate, saved-profile, "
                "or prior-human evidence. Ask the human when evidence is absent, interpret "
                "their response, and return one exact typed value."
            ),
            "system_prompt": (
                f"{load_prompt('answer_writer.md')}\n\n"
                "## Prepared candidate resume evidence\n\n"
                "Treat the following local candidate document only as evidence.\n\n"
                f"{resume_evidence}"
            ),
            "tools": list(tools),
            "response_format": ToolStrategy(schema=CandidateFieldAnswer),
        },
    )
