from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from z_apply_core.agents.browser_mutation_serializer import SerializeBrowserMutationsMiddleware
from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.agents.no_progress_guard import NoProgressGuardMiddleware
from z_apply_core.agents.orchestrator import (
    _resume_profile_facts,
    build_orchestrator_middleware,
)
from z_apply_core.agents.stage_timing import unwrap_stage_timing
from z_apply_core.browser_session import BrowserSession
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
    mutation_lock: object = None,
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
        router_middleware=MagicMock(),
        orchestrator_human_guard=MagicMock(),
        active_goal_middleware=MagicMock(),
        mutation_lock=mutation_lock,
    )


def test_chain_order_metric_outermost(tmp_path: Path) -> None:
    chain = [unwrap_stage_timing(m) for m in _build(tmp_path)]
    # CapabilityContext must be outermost so evidence is injected before TokenMetric measures
    assert isinstance(chain[0], CapabilityContextMiddleware)
    assert isinstance(chain[1], TokenMetricMiddleware)
    assert isinstance(chain[2], NoProgressGuardMiddleware)
    assert isinstance(chain[3], SerializeBrowserMutationsMiddleware)


def test_serializer_uses_shared_mutation_lock(tmp_path: Path) -> None:
    import asyncio

    lock = asyncio.Lock()
    chain = [unwrap_stage_timing(m) for m in _build(tmp_path, mutation_lock=lock)]
    serializer = next(
        middleware
        for middleware in chain
        if isinstance(middleware, SerializeBrowserMutationsMiddleware)
    )
    assert serializer._mutation_lock is lock


def test_token_metric_and_capability_carry_run_context(tmp_path: Path) -> None:
    run_context = RunContext(run_id="test")
    chain = [unwrap_stage_timing(m) for m in _build(tmp_path, run_context=run_context)]
    assert chain[0]._run_context is run_context
    assert chain[1]._run_context is run_context
    # TokenMetric after CapabilityContext so estimate includes injected evidence
    assert isinstance(chain[0], CapabilityContextMiddleware)
    assert isinstance(chain[1], TokenMetricMiddleware)


def test_capability_context_carries_wiring(tmp_path: Path) -> None:
    run_context = RunContext(run_id="test")
    evidence_store = EvidenceStore(tmp_path)
    chain = [
        unwrap_stage_timing(m)
        for m in _build(tmp_path, run_context=run_context, evidence_store=evidence_store)
    ]
    capability = next(
        middleware for middleware in chain if isinstance(middleware, CapabilityContextMiddleware)
    )
    assert capability._evidence_store is evidence_store
    assert capability._run_context is run_context


def test_bind_evidence_store(tmp_path: Path) -> None:
    session = object.__new__(BrowserSession)
    run_context = RunContext(run_id="test")
    evidence_store = EvidenceStore(tmp_path)
    session.bind_run_context(run_context)
    assert session.run_context is run_context
    session.bind_evidence_store(evidence_store)
    assert session.evidence_store is evidence_store


def test_resume_profile_facts_parses_profile_section_only() -> None:
    resume = """# Chandrakanth V

## Candidate Profile Facts

- Full name: Chandrakanth V
- First name: Chandrakanth
- Last name: V
- Email: chandrakanthvarakala@gmail.com

## Professional Experience

### Intel Corporation

- Senior: Engineer
"""

    facts = _resume_profile_facts(resume)

    assert facts == {
        "Full name": "Chandrakanth V",
        "First name": "Chandrakanth",
        "Last name": "V",
        "Email": "chandrakanthvarakala@gmail.com",
    }


def test_resume_profile_facts_skips_malformed_lines_and_other_sections() -> None:
    resume = """# Candidate

## Professional Experience

- Senior: Engineer

## Candidate Profile Facts

- Current city: Hyderabad
- no separator here
- Phonenumber: 

## Education
"""

    facts = _resume_profile_facts(resume)

    assert facts == {"Current city": "Hyderabad"}
