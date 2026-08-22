"""Silent summarization LLM calls must emit run events.

The deepagents summarizer sits OUTSIDE z-apply-core's middleware chain, so its
full-history LLM calls used to be invisible (minutes of zero-event dead air).
The observability seam wraps the summarizer's model and emits started/
completed events — including durations on the error path.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from z_apply_core.agents.summarization_observability import (
    ObservedSummaryModel,
    install_summarization_observability,
    reset_summarization_observer,
    set_summarization_observer,
)


class ScriptedModel(BaseChatModel):
    """Records invocations; optionally raises to exercise the error path."""

    fail: bool = False
    delay: float = 0.0
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider exploded")
        return ChatResult(generations=[ChatGeneration(message=HumanMessage(content="summary"))])


def _collect_events():
    events: list[tuple[str, dict[str, Any]]] = []

    async def observe(event_name: str, data: dict[str, Any]) -> None:
        events.append((event_name, data))

    return events, observe


def test_installed_summarizers_wrap_their_model() -> None:
    install_summarization_observability()
    from deepagents import graph as deepagents_graph
    from deepagents.backends import FilesystemBackend
    from deepagents.middleware.summarization import create_summarization_middleware

    inner = ScriptedModel()
    middleware = deepagents_graph.create_summarization_middleware(
        inner, FilesystemBackend(root_dir="/tmp", virtual_mode=True)
    )
    assert isinstance(middleware.model, ObservedSummaryModel)
    assert middleware.model.inner_model is inner
    # Behavior invariants preserved: same trigger/keep defaults as raw factory.
    reference = create_summarization_middleware(
        ScriptedModel(), FilesystemBackend(root_dir="/tmp", virtual_mode=True)
    )
    assert middleware._lc_helper.trigger == reference._lc_helper.trigger
    assert middleware._lc_helper.keep == reference._lc_helper.keep


def test_async_summary_call_emits_started_and_completed_with_duration() -> None:
    events, observer = _collect_events()
    token = set_summarization_observer(observer)
    try:
        observed = ObservedSummaryModel(ScriptedModel(delay=0.05))
        result = asyncio.run(observed.ainvoke("summarize this history"))
    finally:
        reset_summarization_observer(token)

    assert "summary" in str(result.content)
    names = [name for name, _ in events]
    assert names == ["summarization_model_call_started", "summarization_model_call_completed"]
    completed_data = events[1][1]
    assert completed_data["status"] == "ok"
    assert completed_data["duration_ms"] >= 40
    assert completed_data["n_messages"] >= 1
    assert completed_data["est_tokens"] > 0


def test_failed_summary_call_still_emits_completed_with_error_status() -> None:
    events, observer = _collect_events()
    token = set_summarization_observer(observer)
    try:
        observed = ObservedSummaryModel(ScriptedModel(fail=True))
        try:
            asyncio.run(observed.ainvoke("summarize"))
            raised = False
        except RuntimeError:
            raised = True
    finally:
        reset_summarization_observer(token)

    assert raised  # delegation stays transparent: the error still propagates
    names = [name for name, _ in events]
    assert names == ["summarization_model_call_started", "summarization_model_call_completed"]
    assert events[1][1]["status"] == "error"
    assert events[1][1]["error_type"] == "RuntimeError"
    assert isinstance(events[1][1]["duration_ms"], int)


def test_no_observer_set_is_silent_passthrough() -> None:
    observed = ObservedSummaryModel(ScriptedModel())
    result = asyncio.run(observed.ainvoke("anything"))
    assert "summary" in str(result.content)


def test_profile_and_attributes_delegate_to_inner_model() -> None:
    inner = ScriptedModel()
    inner.profile = {"max_input_tokens": 12345}
    observed = ObservedSummaryModel(inner)
    assert observed.profile == {"max_input_tokens": 12345}
    assert observed._llm_type == "scripted"
