"""Probe: what does CapabilityContextMiddleware leave visible to the orchestrator?

Runs the real middleware's awrap_model_call against the production-shaped
toolset with a stub browser and prints the post-filter tool list.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from z_apply_core.agents.capability_context import CapabilityContextMiddleware

captured: dict[str, Any] = {}


class _StubObservation:
    revision = 1

    def bounded_render(self, budget_chars: int = 0) -> str:
        return "stub"


class _StubBrowser:
    pending_atomic_upload_target = None
    current_observation = _StubObservation()

    async def inspect_capabilities(self):
        return None


def _dummy(name: str):
    @tool
    async def _t() -> str:
        """dummy"""
        return "ok"

    _t.name = name  # type: ignore[misc]
    return _t


@tool(return_direct=True)
async def application_blocked(reason: str) -> str:
    """Block."""
    return "blocked"


@tool(return_direct=True)
async def application_submitted(confirmation: str) -> str:
    """Submit."""
    return "submitted"


@tool
async def report_job_metadata(company: str, role: str, location: str | None = None) -> str:
    """Record the company and role for this application on the run view."""
    return f"Recorded job metadata: company={company}, role={role}."


BROWSER_TOOLS = [
    "browser_navigate", "browser_snapshot", "browser_find", "browser_click",
    "browser_type", "browser_fill_form", "browser_select_option",
    "browser_evaluate", "browser_tabs", "browser_wait_for",
    "browser_handle_dialog",
]

ALL_TOOLS = [
    *[_dummy(n) for n in BROWSER_TOOLS],
    _dummy("remember_platform_lesson"),
    _dummy("lookup_candidate_memory"),
    _dummy("ask_human"),
    application_blocked,
    application_submitted,
    report_job_metadata,
]


async def main() -> None:
    mw = CapabilityContextMiddleware(_StubBrowser())

    async def handler(request):
        captured["tools"] = [
            t.name if hasattr(t, "name") else t.get("function", {}).get("name", str(t))
            for t in (request.tools or [])
        ]

    request = ModelRequest(model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])), messages=[HumanMessage(content="probe")], tools=list(ALL_TOOLS))
    await mw.awrap_model_call(request, handler)
    names = sorted(captured.get("tools") or [])
    print(f"post-filter tool count: {len(names)}")
    for n in names:
        print(" -", n)
    print(f"\nreport_job_metadata visible: {'report_job_metadata' in names}")


asyncio.run(main())
