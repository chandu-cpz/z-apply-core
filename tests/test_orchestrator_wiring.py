from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from z_apply_core.agents.candidate_field import CandidateFieldMiddleware
from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.agents.form_phase_controller import FormPhaseController
from z_apply_core.agents.orchestrator import build_orchestrator_middleware
from z_apply_core.agents.safe_tool_batch import SafeToolBatchMiddleware
from z_apply_core.browser_session import BrowserSession
from z_apply_core.context.context_budget import ContextBudgetMiddleware
from z_apply_core.context.evidence_store import EvidenceStore
from z_apply_core.context.run_context import RunContext
from z_apply_core.context.token_metric import TokenMetricMiddleware
from z_apply_core.memory.platform_playbooks import PlatformPlaybooks


class FakeSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def accept(self, event: object) -> None:
        self.events.append(event)


def _build(
    tmp_path: Path,
    *,
    run_context: RunContext | None = None,
    evidence_store: EvidenceStore | None = None,
):
    resolved_run_context = run_context or RunContext(run_id="test")
    resolved_evidence_store = evidence_store or EvidenceStore(tmp_path)
    return build_orchestrator_middleware(
        run_context=resolved_run_context,
        evidence_store=resolved_evidence_store,
        event_sink=FakeSink(),
        active_browser=None,
        platform_playbooks=PlatformPlaybooks(),
        job_url="https://example.test/apply",
        context_inbox=None,
        candidate_memory=None,
        router_middleware=MagicMock(),
        form_phase_controller=FormPhaseController(),
        orchestrator_human_guard=MagicMock(),
        active_goal_middleware=MagicMock(),
        terminal=None,
    )


def test_chain_order_budget_outermost(tmp_path: Path) -> None:
    chain = _build(tmp_path)
    assert isinstance(chain[0], ContextBudgetMiddleware)
    assert isinstance(chain[1], TokenMetricMiddleware)
    assert isinstance(chain[2], CapabilityContextMiddleware)
    assert isinstance(chain[3], SafeToolBatchMiddleware)


def test_budget_and_token_metric_carry_run_context(tmp_path: Path) -> None:
    run_context = RunContext(run_id="test")
    chain = _build(tmp_path, run_context=run_context)
    assert chain[0]._run_context is run_context
    assert chain[1]._run_context is run_context


def test_candidate_field_carries_run_context(tmp_path: Path) -> None:
    chain = _build(tmp_path)
    candidate = next(
        middleware
        for middleware in chain
        if isinstance(middleware, CandidateFieldMiddleware)
    )
    assert candidate._executor.on_applied is not None


def test_capability_context_carries_wiring(tmp_path: Path) -> None:
    run_context = RunContext(run_id="test")
    evidence_store = EvidenceStore(tmp_path)
    chain = _build(tmp_path, run_context=run_context, evidence_store=evidence_store)
    capability = next(
        middleware
        for middleware in chain
        if isinstance(middleware, CapabilityContextMiddleware)
    )
    assert capability._evidence_store is evidence_store
    assert capability._form_phase_controller is not None
    assert capability._run_context is run_context
    assert capability._form_phase_emit is not None


def test_bind_evidence_store(tmp_path: Path) -> None:
    session = object.__new__(BrowserSession)
    run_context = RunContext(run_id="test")
    evidence_store = EvidenceStore(tmp_path)
    session.bind_run_context(run_context)
    assert session.run_context is run_context
    session.bind_evidence_store(evidence_store)
    assert session.evidence_store is evidence_store
