"""Probe: does report_job_metadata survive create_deep_agent's tool pipeline?

Records request.tools at the middleware layer — exactly what the model sees
after all framework middleware has run.
"""

from __future__ import annotations

import contextlib
from typing import Any

from deepagents import create_deep_agent  # noqa: E402
from langchain.agents.middleware.types import (  # noqa: E402
    AgentMiddleware,
    AgentState,
    ContextT,
    ResponseT,
)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from z_apply_core.agents.harness_profile import configure_z_apply_harness_profile  # noqa: E402

configure_z_apply_harness_profile()

captured: dict[str, Any] = {}


class _ProbeStop(Exception):
    pass


class ToolRecorder(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    def wrap_model_call(self, request, handler):
        captured["tools"] = [
            getattr(t, "name", None) or t.get("function", {}).get("name", str(t))
            for t in (request.tools or [])
        ]
        raise _ProbeStop

    async def awrap_model_call(self, request, handler):
        return self.wrap_model_call(request, handler)


@tool
async def report_job_metadata(company: str, role: str, location: str | None = None) -> str:
    """Record the company and role for this application on the run view."""
    return f"Recorded job metadata: company={company}, role={role}."


def _dummy(name: str):
    @tool
    async def _t() -> str:
        """dummy"""
        return "ok"

    _t.name = name  # type: ignore[misc]
    return _t


BROWSER_TOOLS = [
    "browser_batched", "browser_click", "browser_click_upload", "browser_evaluate",
    "browser_file_upload", "browser_fill_form", "browser_find", "browser_handle_dialog",
    "browser_navigate", "browser_revision", "browser_select_option", "browser_snapshot",
    "browser_tabs", "browser_type", "browser_wait_for",
]


@tool(return_direct=True)
async def application_blocked(reason: str) -> str:
    """Block the application."""
    return "blocked"


@tool(return_direct=True)
async def application_submitted(confirmation: str) -> str:
    """Submit the application."""
    return "submitted"


model = GenericFakeChatModel(messages=iter([AIMessage(content="done")]))

agent = create_deep_agent(
    model=model,
    tools=[
        *[_dummy(n) for n in BROWSER_TOOLS],
        _dummy("lookup_candidate_memory"),
        _dummy("ask_human"),
        application_blocked,
        application_submitted,
        report_job_metadata,
    ],
    system_prompt="probe",
    middleware=[ToolRecorder()],
)

with contextlib.suppress(_ProbeStop):
    agent.invoke({"messages": [{"role": "user", "content": "probe"}]})
names = sorted(captured.get("tools") or [])
print(f"model-visible tool count: {len(names)}")
for n in names:
    print(" -", n)
present = "report_job_metadata" in names
print(f"\nreport_job_metadata visible to model: {present}")
