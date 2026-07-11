from __future__ import annotations

from typing import Literal

from langchain_core.tools import BaseTool, tool

from z_apply_core.human.channel import HumanChannel


def make_human_tools(channel: HumanChannel) -> list[BaseTool]:
    @tool
    async def ask_human(
        question: str,
        reason: Literal["missing_candidate_fact", "ambiguous_field", "human_challenge"],
        field_label: str = "",
        field_evidence: str = "",
        context: str = "",
        url: str = "",
        company_name: str = "System",
        role_name: str = "Application",
        options: list[str] | None = None,
    ) -> dict[str, str]:
        """Ask the human for missing or ambiguous information and wait for the answer.

        reason: one of 'missing_candidate_fact', 'ambiguous_field', 'human_challenge'.
        field_label: the specific required field or fields that need human input.
        field_evidence: current browser evidence showing the field is unresolved.
        """
        answer = await channel.ask(
            question=question,
            context=context,
            url=url,
            company=company_name,
            role=role_name,
            options=options or [],
        )
        return {"human_answer": answer}

    @tool
    async def request_submit_approval(
        final_review: str,
        url: str = "",
        company_name: str = "System",
        role_name: str = "Application",
    ) -> dict[str, str]:
        """Ask the human to approve or reject a review-ready application."""
        approved = await channel.confirm(
            question="Submit this application?",
            context=final_review,
            url=url,
            company=company_name,
            role=role_name,
        )
        return {"submit_approval": "approved" if approved else "rejected"}

    return [ask_human, request_submit_approval]
