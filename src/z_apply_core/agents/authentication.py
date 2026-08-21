from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.harness_profile import (
    DEEPAGENT_FILESYSTEM_PERMISSIONS,
    configure_z_apply_harness_profile,
)
from z_apply_core.agents.no_progress_guard import NoProgressGuardMiddleware
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.providers import ModelGateway
from z_apply_core.agents.result import AuthStatus
from z_apply_core.agents.retry_policy import model_retry_middleware
from z_apply_core.agents.router_middleware import build_router_middleware
from z_apply_core.browser_session import BrowserSession
from z_apply_core.browser_tools import (
    AUTHENTICATION_SPECIALIST_BROWSER_TOOLS,
    make_auth_submit_tool,
    make_verification_link_tool,
)
from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.context.token_metric import TokenMetricMiddleware
from z_apply_core.human.channel import HumanChannel
from z_apply_core.human.tools import make_manual_auth_tool
from z_apply_core.log_labels import node_info
from z_apply_core.paths import CORE_ROOT
from z_apply_core.paths import captcha_path as paths_captcha_path
from z_apply_core.stream_events import FrameworkEventSink, _emit_usage_sync

logger = logging.getLogger(__name__)

# One canonical system prompt shared by the pre-flight authentication node and
# the mid-run AuthenticationSpecialist subagent.
AUTHENTICATION_SYSTEM_PROMPT = load_prompt("authentication_specialist.md")

# Browser tools the authentication agent is allowed to drive. The agent is
# text-only and the supervised browser does not retain screenshot images, so
# browser_take_screenshot is deliberately excluded: it only wastes turn budget
# on visual_evidence_unavailable responses. Email verification links go
# through browser_verify_link, so browser_tabs is excluded too.
AUTHENTICATION_BROWSER_TOOLS = tuple(
    name
    for name in AUTHENTICATION_SPECIALIST_BROWSER_TOOLS
    if name not in {"browser_tabs", "browser_take_screenshot"}
)


@dataclass(frozen=True, slots=True)
class AuthenticationRun:
    summary: str
    model_id: str
    status: AuthStatus


def captcha_artifact_path(run_id: str) -> str:
    """Best-effort captcha screenshot path attached to a manual-auth handoff."""
    return str(paths_captcha_path(run_id).resolve())


def parse_authentication_result(text: str) -> AuthStatus:
    """Map the shared prose contract to the pre-flight auth verdict.

    The single authentication agent finishes with one of
    ``AUTHENTICATED``/``GATE_RESOLVED``/``BLOCKED``. ``not_verified`` covers an
    agent that stopped without a marker: the orchestrator re-observes the live
    page and can still delegate the same specialist mid-run.
    """
    upper = text.upper()
    if "AUTHENTICATED" in upper or "GATE_RESOLVED" in upper:
        return "authenticated"
    if "BLOCKED" in upper:
        return "blocked"
    return "not_verified"


def build_authentication_tools(
    *,
    browser_tools: Sequence[BaseTool],
    submit_auth_form: Callable[[str], Awaitable[str]] | None = None,
    open_verification_link: Callable[[str], Awaitable[str]] | None = None,
    gmail_tools: Sequence[BaseTool] = (),
    human_channel: HumanChannel | None = None,
) -> list[BaseTool]:
    """One canonical tool set for the authentication agent (both call sites).

    Used by the pre-flight node and the mid-run AuthenticationSpecialist so a
    login, CAPTCHA, OTP, or account gate resolves identically everywhere. The
    manual-auth handoff carries no challenge screenshot: the human watches the
    live browser (VNC/web) and only needs the notification.
    """
    allowed = set(AUTHENTICATION_BROWSER_TOOLS)
    tools: list[BaseTool] = [
        tool for tool in browser_tools if getattr(tool, "name", "") in allowed
    ]
    if submit_auth_form is not None:
        tools.append(make_auth_submit_tool(submit_auth_form))
    if open_verification_link is not None:
        tools.append(make_verification_link_tool(open_verification_link))
    tools.extend(gmail_tools)
    if human_channel is not None:
        tools.append(make_manual_auth_tool(human_channel))
    return tools


class AuthenticationBudgetMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Enforce the authentication agent's hard wait budget.

    ``browser_wait_for`` is capped at one call per run: a page that is not
    usable after a single short settle is either still loading scaffolding
    (take fresh evidence) or blocked by a challenge (escalate to the human).
    Looping on identical waits is a stall, not progress.
    """

    def __init__(self, *, max_waits: int = 1) -> None:
        super().__init__()
        self._max_waits = max(1, max_waits)
        self._waits = 0

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        if str(request.tool_call.get("name", "")) != "browser_wait_for":
            return await handler(request)
        if self._waits >= self._max_waits:
            return ToolMessage(
                content=(
                    "RUNTIME WAIT-BUDGET EXHAUSTED: browser_wait_for was already used "
                    f"the allowed {self._max_waits} time(s) in this authentication run. "
                    "Do not wait again. Take fresh browser evidence now. If a "
                    "CAPTCHA/verification challenge is visible, call "
                    "request_manual_auth instead of waiting."
                ),
                name="browser_wait_for",
                tool_call_id=str(request.tool_call.get("id", "")),
                status="error",
            )
        self._waits += 1
        return await handler(request)


def make_authentication_no_progress_guard(
    browser: BrowserSession | None,
) -> NoProgressGuardMiddleware:
    """No-progress circuit for the auth agent: precise, not blunt.

    The auth phase is legitimately read-heavy: locating email/password/submit
    controls with browser_find and re-snapshotting after each action does not
    change the browser signature on every call. True loops are already caught
    precisely by the identical-read denial and the action-window repetition
    detector, so the stagnant-call counter gets a generous cap instead of
    killing the turn mid-login-fill.
    """
    return NoProgressGuardMiddleware(
        browser=browser,
        max_stagnant_tool_calls=20,
        max_identical_denials=2,
        max_non_progress=6,
    )


async def run_authentication_agent(
    *,
    task: str,
    tools: Sequence[BaseTool],
    config: RunnableConfig,
    sink: FrameworkEventSink | None = None,
    provider: ModelGateway | None = None,
    ledger: RunCallLedger | None = None,
    browser: BrowserSession | None = None,
) -> AuthenticationRun:
    """Run the single shared authentication agent for one bounded task.

    This is the same agent (same prompt, tools, and budgets) that the
    orchestrator dispatches mid-run as the AuthenticationSpecialist subagent;
    here it runs as the pre-flight node's root agent.
    """
    configure_z_apply_harness_profile()
    if provider is None:
        return AuthenticationRun(
            "Model routing failed: no model provider was provided.", "", "failed"
        )
    try:
        llm = provider.get_model(thinking_effort="low")
    except (ImportError, ValueError) as exc:
        return AuthenticationRun(f"Model selection failed: {exc}", "", "failed")

    node_info(logger, "authenticate_default_account", "initial model: %s", provider.model_id)
    router_middleware = build_router_middleware(
        provider,
        role="authenticate_default_account",
        sink=sink,
        ledger=ledger,
    )
    usage_emit = lambda event: _emit_usage_sync(sink, event)  # noqa: E731

    agent = create_deep_agent(
        model=llm,
        tools=list(tools),
        system_prompt=AUTHENTICATION_SYSTEM_PROMPT,
        middleware=[
            TokenMetricMiddleware(agent="authenticate_default_account", emit=usage_emit),
            AuthenticationBudgetMiddleware(max_waits=1),
            make_authentication_no_progress_guard(browser),
            *model_retry_middleware(provider),
            router_middleware,
            ProseToolCallGuardMiddleware(),
        ],
        backend=FilesystemBackend(root_dir=CORE_ROOT, virtual_mode=True),
        permissions=DEEPAGENT_FILESYSTEM_PERMISSIONS,
    )

    run_config = cast(RunnableConfig, config.copy() if config else {})
    result = await consume_deepagent_stream(
        agent.astream_events(
            cast(
                Any,
                {"messages": [{"role": "user", "content": task}]},
            ),
            config=run_config,
            version="v3",
        ),
        sink=sink,
        root_source="authenticate_default_account",
    )
    summary = _final_text(result.output)
    return AuthenticationRun(
        summary=summary or "Authentication controller stopped without a result marker.",
        model_id=router_middleware.last_model_id,
        status=parse_authentication_result(summary),
    )


def _final_text(output: dict[str, Any]) -> str:
    messages = output.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""
