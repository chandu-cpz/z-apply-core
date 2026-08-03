from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.browser_observation import BrowserCapabilities, BrowserObservation
from z_apply_core.context.evidence_store import EvidenceStore
from z_apply_core.context.run_context import RunContext


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
async def test_observation_none_is_graceful() -> None:
    run_context = RunContext(run_id="run-3")
    browser = FakeBrowser(None)
    middleware = CapabilityContextMiddleware(
        browser,
        run_context=run_context,
    )
    request = _request()
    received: list[ModelRequest[Any]] = []

    result = await middleware.awrap_model_call(request, _handler(received))

    assert result.result[0].content == "done"
    last = received[0].messages[-1]
    assert isinstance(last, HumanMessage)
    assert "CURRENT BROWSER EVIDENCE" not in str(last.content)
