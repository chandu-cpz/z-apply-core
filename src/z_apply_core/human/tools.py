from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from langchain_core.tools import BaseTool, tool

from z_apply_core.human.channel import HumanChannel
from z_apply_core.memory.applicant_memory import CandidateMemory

logger = logging.getLogger(__name__)


def _coerce_options(options: list[str] | str | None) -> list[str] | None:
    """Normalize a model-supplied options value into a list of strings.

    Weaker models sometimes pass the JSON string ``"[]"`` instead of a real
    list, which used to fail tool validation and abort ``ask_human`` before the
    human was ever asked. A real list passes through; a parseable JSON list or
    scalar string is converted; anything else degrades to no options rather
    than failing the human request.
    """
    if options is None:
        return None
    if isinstance(options, list):
        return [str(item) for item in options if str(item).strip()]
    stripped = options.strip()
    if not stripped or stripped in {"[]", "null", "None"}:
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed]
    return None


def make_manual_auth_tool(
    channel: HumanChannel,
    *,
    human_challenge_image_path: str = "",
    max_attempts: int = 2,
) -> BaseTool:
    """Create one fixed, credential-safe manual authentication handoff.

    ``max_attempts`` bounds how many times the tool may be invoked within one
    agent run: a challenge the human cannot resolve after two asks must be
    reported as a ``BLOCKED`` result instead of looping on the human.
    """
    attempts = 0

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
        nonlocal attempts
        attempts += 1
        if attempts > max_attempts:
            return {
                "manual_auth": "exhausted",
                "error": (
                    f"request_manual_auth was already called {max_attempts} times for "
                    "this gate and the human could not resolve it. Stop asking. Finish "
                    "with a BLOCKED result naming the concrete unresolved dependency."
                ),
            }
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
    capture_human_challenge: Callable[[str], Awaitable[Path]] | None = None,
    allow_human_challenge: bool = True,
) -> list[BaseTool]:
    """Build the ask_human tool for one agent.

    ``allow_human_challenge=False`` strips the browser challenge-capture path
    (``capture_human_challenge`` becomes unreachable and a ``human_challenge``
    request returns a typed error instead of raising). Use it for subagents that
    must never drive the browser, so a misclassified upload control or CAPTCHA
    can never turn into an unhandled browser-locator exception that kills the
    subagent run.
    """
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
        options: list[str] | str | None = None,
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
            if not allow_human_challenge or capture_human_challenge is None:
                return {
                    "human_answer": "",
                    "error": (
                        "human_challenge capture is unavailable for this agent; "
                        "challenge screenshots can only be requested by the "
                        "orchestrator. If you meant a candidate field, use reason "
                        "missing_candidate_fact or ambiguous_field with a field_label."
                    ),
                }
            try:
                resolved_image_path = str(await capture_human_challenge(challenge_target))
            except Exception as exc:  # noqa: BLE001 - never crash the run on a bad target
                return {
                    "human_answer": "",
                    "error": (
                        f"Challenge capture failed for target {challenge_target!r}: "
                        f"{exc}. Pass the exact current element ref from fresh "
                        "browser evidence, never visible button text, then retry."
                    ),
                }
        try:
            answer = await channel.ask(
                question=question,
                context=context,
                url=url,
                company=company_name,
                role=role_name,
                options=_coerce_options(options) or [],
                image_path=resolved_image_path,
                field_label=field_label,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 - a channel failure must never kill the run
            logger.warning("ask_human channel failed for field %r: %s", field_label, exc)
            return {
                "human_answer": "",
                "error": (
                    f"The human channel could not deliver the question: {exc}. "
                    "Retry the ask_human call once more; if it keeps failing, "
                    "report the field as blocked."
                ),
            }
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

    return [ask_human]
