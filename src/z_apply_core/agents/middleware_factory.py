from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from z_apply_core.agents.browser_mutation_serializer import SerializeBrowserMutationsMiddleware
from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.agents.context_inbox import ContextInbox, ContextInboxMiddleware
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.no_progress_guard import NoProgressGuardMiddleware
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.retry_policy import model_retry_middleware
from z_apply_core.agents.router_middleware import ModelRouter
from z_apply_core.agents.stage_timing import wrap_chain_with_stage_timing
from z_apply_core.browser_session import BrowserSession
from z_apply_core.context.evidence_store import EvidenceStore
from z_apply_core.context.run_context import RunContext
from z_apply_core.context.token_metric import TokenMetricMiddleware
from z_apply_core.memory.platform_playbooks import PlatformPlaybooks
from z_apply_core.stream_events import FrameworkEventSink, SequencedEventSink, _emit_usage_sync


def build_agent_middleware(
    *,
    role: str,
    provider: Any | None = None,
    run_context: RunContext | None = None,
    evidence_store: EvidenceStore | None = None,
    event_sink: SequencedEventSink | FrameworkEventSink | None = None,
    active_browser: BrowserSession | None = None,
    platform_playbooks: PlatformPlaybooks | None = None,
    job_url: str = "",
    context_inbox: ContextInbox | None = None,
    router_middleware: ModelRouter,
    human_guard: HumanEscalationGuardMiddleware | None = None,
    no_progress_kwargs: dict[str, Any] | None = None,
    extra_middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
    mutation_lock: asyncio.Lock | None = None,
) -> list[AgentMiddleware[Any, Any, Any]]:
    """Single source of truth for model-call middleware ordering.

    Fixed skeleton (outermost -> innermost):
      CapabilityContext (injects evidence) -> TokenMetric (measures what model sees)
      -> ContextInbox -> NoProgress -> SerializeMutations -> SubagentDispatch/extra
      -> Retry -> Router -> ProseGuard -> HumanGuard -> ActiveGoal

    Previous bug: TokenMetric was outermost, so its "before" estimate missed
    CapabilityContext's injected evidence. Now TokenMetric wraps *after*
    capability injection so input_tokens_estimate matches actual prompt.
    """

    def usage_emit(event: object) -> None:
        _emit_usage_sync(event_sink, event)

    # TokenMetric must be AFTER CapabilityContext so estimate includes injected evidence
    # but still outermost for actual timing — so we place it just after capability.
    chain: list[AgentMiddleware[Any, Any, Any]] = []

    # Capability first so every later middleware sees evidence
    chain.append(
        CapabilityContextMiddleware(
            active_browser,
            platform_playbooks=platform_playbooks,
            job_url=job_url,
            run_context=run_context,
            evidence_store=evidence_store,
            event_sink=event_sink,
            role=role,
        )
    )
    # Now measure what the model will actually receive
    if run_context is not None:
        chain.append(TokenMetricMiddleware(agent=role, run_context=run_context, emit=usage_emit))
    else:
        # Browserless subagents (AnswerWriter etc.) still get metric but without RunContext totals
        chain.append(TokenMetricMiddleware(agent=role, emit=usage_emit))

    if context_inbox is not None:
        chain.append(ContextInboxMiddleware(context_inbox))

    # NoProgress with role-tuned kwargs
    if no_progress_kwargs is not None:
        chain.append(
            NoProgressGuardMiddleware(
                browser=active_browser,
                on_no_progress=router_middleware.reject_active_response,
                **no_progress_kwargs,
            )
        )
    else:
        chain.append(
            NoProgressGuardMiddleware(
                browser=active_browser, on_no_progress=router_middleware.reject_active_response
            )
        )

    chain.append(SerializeBrowserMutationsMiddleware(sink=event_sink, lock=mutation_lock))
    chain.extend(extra_middleware)
    chain.extend(model_retry_middleware(provider))
    chain.append(router_middleware)
    chain.append(ProseToolCallGuardMiddleware())
    if human_guard is not None:
        chain.append(human_guard)
    # DEC-010: time each awrap_model_call stage; emit model_phase events when a
    # stage crosses the threshold (FAIL-006 inter-turn hole observability).
    return wrap_chain_with_stage_timing(chain, sink=event_sink, role=role)
