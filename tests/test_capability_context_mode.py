"""DEC-017: Z_APPLY_CAPABILITY_CONTEXT_MODE — full | no-counters | off.

Default must be byte-identical to the historical rendering; no-counters strips
only aggregate count lines; off skips inspection/injection entirely.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.browser_observation import BrowserCapabilities


class _StubObservation:
    revision = 1
    signature = "sig"
    url = "https://example.test/x"
    title = "stub"
    evidence = "evidence " * 50

    def bounded_render(self, budget_chars: int = 0) -> str:
        return self.evidence

    def render(self) -> str:
        return self.evidence


class _ModeBrowser:
    pending_atomic_upload_target = None

    def __init__(self, caps: BrowserCapabilities | None, *, inspect_calls: int = 0) -> None:
        self.current_observation = _StubObservation()
        self._caps = caps
        self.inspect_calls = inspect_calls

    async def inspect_capabilities(self):
        self.inspect_calls += 1
        return self._caps


def _request() -> ModelRequest:
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
        messages=[HumanMessage(content="probe")],
        tools=[],
    )


def _drive(middleware: CapabilityContextMiddleware) -> tuple[str | None, list]:
    captured: dict[str, Any] = {}

    async def handler(request):
        captured["messages"] = [str(m.content) for m in request.messages]
        return "done"

    asyncio.run(
        middleware.awrap_model_call(
            _request(),
            handler,
        )
    )
    return (captured["messages"][-1] if "messages" in captured else None), []


CAPS = BrowserCapabilities(
    editable_controls_visible=True,
    unresolved_required_controls=2,
    unresolved_names=("Expected Salary", "Notice Period"),
    auth_gate_visible=False,
    required_file_upload_pending=False,
)


def test_full_mode_is_default_and_renders_counts(monkeypatch: Any) -> None:
    monkeypatch.delenv("Z_APPLY_CAPABILITY_CONTEXT_MODE", raising=False)
    browser = _ModeBrowser(CAPS)
    middleware = CapabilityContextMiddleware(browser)  # type: ignore[arg-type]

    context, _ = _drive(middleware)

    assert context is not None
    assert "unresolved_required_controls=2" in context
    assert "invalid_controls=0" in context
    assert "(Expected Salary*, Notice Period*)" in context
    assert browser.inspect_calls == 1


def test_no_counters_strips_aggregates_keeps_fields_and_upload_state() -> None:
    browser = _ModeBrowser(CAPS)
    middleware = CapabilityContextMiddleware(browser, context_mode="no-counters")  # type: ignore[arg-type]

    context, _ = _drive(middleware)

    assert context is not None
    assert "unresolved_required_controls=" not in context
    assert "invalid_controls=" not in context
    # per-field rows survive so the agent can still act on them
    assert "unresolved_fields: Expected Salary*, Notice Period*" in context
    # upload state survives
    assert "empty_file_upload_present=false" in context
    assert "required_file_upload_pending=false" in context


def test_off_mode_skips_inspection_and_injection_entirely() -> None:
    caps_browser = _ModeBrowser(CAPS)
    middleware = CapabilityContextMiddleware(caps_browser, context_mode="off")  # type: ignore[arg-type]

    context, _ = _drive(middleware)

    assert context == "probe", "off mode must pass messages through untouched"
    assert caps_browser.inspect_calls == 0, "off mode must not touch the browser"


def test_invalid_mode_falls_back_to_full_with_warning() -> None:
    middleware = CapabilityContextMiddleware(_ModeBrowser(CAPS), context_mode="bogus")  # type: ignore[arg-type]

    assert middleware._context_mode == "full"


def test_env_var_selects_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("Z_APPLY_CAPABILITY_CONTEXT_MODE", "no-counters")
    middleware = CapabilityContextMiddleware(_ModeBrowser(CAPS))  # type: ignore[arg-type]
    assert middleware._context_mode == "no-counters"

    monkeypatch.setenv("Z_APPLY_CAPABILITY_CONTEXT_MODE", "ALSO-BOGUS")
    fallback = CapabilityContextMiddleware(_ModeBrowser(CAPS))  # type: ignore[arg-type]
    assert fallback._context_mode == "full"
