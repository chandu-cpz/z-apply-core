from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from langchain_core.tools import BaseTool, tool

from z_apply_core.human.channel import HumanChannel
from z_apply_core.memory.applicant_memory import CandidateMemory

logger = logging.getLogger(__name__)


def make_manual_auth_tool(
    channel: HumanChannel,
    *,
    human_challenge_image_path: str = "",
) -> BaseTool:
    """Create one fixed, credential-safe manual authentication handoff."""

    @tool
    async def request_manual_auth(
        challenge_summary: str,
        url: str = "",
        company_name: str = "System",
        role_name: str = "Application",
    ) -> dict[str, str]:
        """Ask the human to complete the visible auth gate in the live browser.

        Never requests credentials in Telegram. The human replies with one
        button after completing the browser action or when unable to continue.
        """
        answer = await channel.ask(
            question=(
                "Please complete the visible authentication or CAPTCHA in the live "
                "browser, then choose one option."
            ),
            context=challenge_summary,
            url=url,
            company=company_name,
            role=role_name,
            options=["Done", "Cannot complete"],
            image_path=human_challenge_image_path,
        )
        return {"manual_auth": "done" if answer == "Done" else "cannot_complete"}

    return request_manual_auth


def make_human_tools(
    channel: HumanChannel,
    *,
    candidate_memory: CandidateMemory | None = None,
    on_answer: Callable[[str], None] | None = None,
    on_approval: Callable[[bool], None] | None = None,
    before_submit_approval: Callable[
        [str, str], Awaitable[dict[str, object] | None]
    ]
    | None = None,
    capture_human_challenge: Callable[[str], Awaitable[Path]] | None = None,
) -> list[BaseTool]:
    answered_fields: dict[str, str] = {}

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
        challenge_target: str = "",
    ) -> dict[str, str]:
        """Ask the human for missing or ambiguous information and wait for the answer.

        reason: one of 'missing_candidate_fact', 'ambiguous_field', 'human_challenge'.
        field_label: the specific required field or fields that need human input.
        field_evidence: current browser evidence showing the field is unresolved.
        challenge_target: current browser ref for a visible human challenge.
        """
        field_key = field_label.strip().casefold()
        if field_key and field_key in answered_fields:
            logger.info("Reusing the answered human request for field %r", field_label)
            return {
                "human_answer": answered_fields[field_key],
                "candidate_memory_stored": "true",
                "human_request_reused": "true",
            }

        resolved_image_path = ""
        if reason == "human_challenge":
            if capture_human_challenge is None:
                raise RuntimeError("human challenge capture is unavailable")
            resolved_image_path = str(await capture_human_challenge(challenge_target))
        answer = await channel.ask(
            question=question,
            context=context,
            url=url,
            company=company_name,
            role=role_name,
            options=options or [],
            image_path=resolved_image_path,
        )
        if on_answer is not None:
            on_answer(field_label)
        if field_key:
            answered_fields[field_key] = answer
        stored = False
        if candidate_memory is not None and reason == "missing_candidate_fact":
            stored = await candidate_memory.remember_human_answer(
                field_label=field_label,
                question=question,
                answer=answer,
            )
        return {"human_answer": answer, "candidate_memory_stored": str(stored).lower()}

    @tool
    async def request_submit_approval(
        final_review: str,
        submission_target: str,
        url: str = "",
        company_name: str = "System",
        role_name: str = "Application",
    ) -> dict[str, object]:
        """Ask the human to approve the review for one exact final submit control."""
        if before_submit_approval is not None:
            gate = await before_submit_approval(final_review, submission_target)
            if gate is not None and gate.get("ready") is False:
                return {
                    "submit_approval": "not_ready",
                    "readiness": gate,
                }
        approved = await channel.confirm(
            question="Submit this application?",
            context=final_review,
            url=url,
            company=company_name,
            role=role_name,
        )
        if on_approval is not None:
            on_approval(approved)
        if approved:
            return {"submit_approval": "approved"}
        correction = await channel.ask(
            question="What should I correct before requesting submission approval again?",
            context=(
                "Submission was not approved. Give one precise correction or say "
                "that the application should be stopped."
            ),
            url=url,
            company=company_name,
            role=role_name,
        )
        return {
            "submit_approval": "rejected",
            "correction": correction,
        }

    return [ask_human, request_submit_approval]
