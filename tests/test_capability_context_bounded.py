from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.agents.form_phase_controller import (
    FormPhaseController,
    FormPhaseEmitter,
)
from z_apply_core.browser_observation import BrowserCapabilities, BrowserObservation
from z_apply_core.context.evidence_store import EvidenceStore
from z_apply_core.context.run_context import RunContext
from z_apply_core.stream_events import FormPhaseEvent


@tool
def browser_observe() -> str:
    """Observe."""
    return "observed"


@tool
def browser_click() -> str:
    """Click."""
    return "clicked"


class FakeBrowser:
    def __init__(self, observation: BrowserObservation | None) -> None:
        self._observation = observation
        self.pending_atomic_upload_target = ""

    async def inspect_capabilities(self) -> BrowserCapabilities:
        return BrowserCapabilities()

    @property
    def current_observation(self) -> BrowserObservation | None:
        return self._observation

    def set_observation(self, observation: BrowserObservation | None) -> None:
        self._observation = observation


class PhaseBumpingController(FormPhaseController):
    def __init__(self) -> None:
        super().__init__()
        self.browser_mutated_flags: list[bool] = []

    async def update_form_phase(
        self,
        run_context: RunContext,
        browser_evidence: str,
        emit: FormPhaseEmitter,
        *,
        browser_mutated: bool = False,
    ) -> None:
        self.browser_mutated_flags.append(browser_mutated)
        if browser_mutated:
            run_context.form_phase.apply_analysis("submitted")
            await emit(
                FormPhaseEvent(
                    run_id=run_context.run_id,
                    phase="submitted",
                    confidence="high",
                )
            )
        return None


class RaisingFormPhaseController(FormPhaseController):
    async def update_form_phase(
        self,
        run_context: RunContext,
        browser_evidence: str,
        emit: FormPhaseEmitter,
        *,
        browser_mutated: bool = False,
    ) -> None:
        raise RuntimeError("form-phase boom")


def _observation(revision: int) -> BrowserObservation:
    return BrowserObservation.create(
        revision=revision,
        url="https://example.test/apply",
        title="Apply",
        evidence='- textbox "Email" [ref=e500]\n- button "Continue" [ref=e501]',
    )


def _request() -> ModelRequest[Any]:
    return ModelRequest(
        model=object(),
        messages=[HumanMessage(content="task")],
        tools=[browser_observe, browser_click],
    )


def _handler(received: list[ModelRequest[Any]]) -> Any:
    async def handler(req: ModelRequest[Any]) -> ModelResponse[Any]:
        received.append(req)
        return ModelResponse(result=[AIMessage(content="done")])

    return handler


async def _run(
    middleware: CapabilityContextMiddleware,
    request: ModelRequest[Any],
) -> ModelResponse[Any]:
    received: list[ModelRequest[Any]] = []
    result = await middleware.awrap_model_call(request, _handler(received))
    return result


@pytest.mark.asyncio
async def test_compact_evidence_fallback_without_store() -> None:
    browser = FakeBrowser(_observation(revision=1))
    middleware = CapabilityContextMiddleware(browser)
    request = _request()
    received: list[ModelRequest[Any]] = []

    await middleware.awrap_model_call(request, _handler(received))

    last = received[0].messages[-1]
    assert isinstance(last, HumanMessage)
    assert "CURRENT BROWSER EVIDENCE" in str(last.content)


@pytest.mark.asyncio
async def test_bounded_evidence_saved_to_store(tmp_path) -> None:
    browser = FakeBrowser(_observation(revision=5))
    store = EvidenceStore(tmp_path)
    middleware = CapabilityContextMiddleware(browser, evidence_store=store)
    request = _request()
    received: list[ModelRequest[Any]] = []

    await middleware.awrap_model_call(request, _handler(received))

    last = received[0].messages[-1]
    assert isinstance(last, HumanMessage)
    assert "CURRENT BROWSER EVIDENCE" not in str(last.content)
    assert (tmp_path / "obs_5.txt").is_file()
    assert store.get("5") is not None


@pytest.mark.asyncio
async def test_form_phase_hook_emits_after_revision_bump() -> None:
    run_context = RunContext(run_id="run-1")
    browser = FakeBrowser(_observation(revision=1))
    controller = PhaseBumpingController()
    emitted: list[FormPhaseEvent] = []

    async def emit(event: FormPhaseEvent) -> None:
        emitted.append(event)

    middleware = CapabilityContextMiddleware(
        browser,
        run_context=run_context,
        form_phase_controller=controller,
        form_phase_emit=emit,
    )

    await _run(middleware, _request())
    assert emitted == []
    assert run_context.form_phase.phase == "initial"
    assert controller.browser_mutated_flags == [False]

    browser.set_observation(_observation(revision=2))
    await _run(middleware, _request())

    assert controller.browser_mutated_flags == [False, True]
    assert len(emitted) == 1
    assert emitted[0].run_id == "run-1"
    assert emitted[0].phase == "submitted"
    assert emitted[0].confidence == "high"
    assert run_context.form_phase.phase == "submitted"


@pytest.mark.asyncio
async def test_form_phase_hook_failure_is_swallowed() -> None:
    run_context = RunContext(run_id="run-2")
    browser = FakeBrowser(_observation(revision=1))
    middleware = CapabilityContextMiddleware(
        browser,
        run_context=run_context,
        form_phase_controller=RaisingFormPhaseController(),
        form_phase_emit=lambda _event: None,
    )
    request = _request()

    result = await _run(middleware, request)

    assert result.result[0].content == "done"
    assert run_context.form_phase.phase == "initial"


@pytest.mark.asyncio
async def test_observation_none_is_graceful() -> None:
    run_context = RunContext(run_id="run-3")
    browser = FakeBrowser(None)
    middleware = CapabilityContextMiddleware(
        browser,
        run_context=run_context,
        form_phase_controller=PhaseBumpingController(),
        form_phase_emit=lambda _event: None,
    )
    request = _request()
    received: list[ModelRequest[Any]] = []

    result = await middleware.awrap_model_call(request, _handler(received))

    assert result.result[0].content == "done"
    last = received[0].messages[-1]
    assert isinstance(last, HumanMessage)
    assert "CURRENT BROWSER EVIDENCE" not in str(last.content)
