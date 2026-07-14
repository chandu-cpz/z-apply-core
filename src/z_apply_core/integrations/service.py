"""Long-lived Core service used by local transports.

This module deliberately converts graph trace events at the boundary.  It never
passes graph state, raw LangChain events, or browser objects to callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import mimetypes
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from z_apply_core.agents.context_inbox import ContextInbox, ContextMessage
from z_apply_core.browser_session import ARTIFACT_ROOT
from z_apply_core.browser_workspace import BrowserWorkspace
from z_apply_core.graph import make_router, run_job
from z_apply_core.human.broker import (
    BrokeredHumanChannel,
    BrokerRequest,
    HumanRequestBroker,
)
from z_apply_core.human.factory import make_configured_human_channel
from z_apply_core.integrations.events import CoreEventSink, EventBroadcaster
from z_apply_core.integrations.exceptions import (
    BrowserControlConflict,
    BrowserUnavailable,
    CoreShuttingDown,
    HumanRequestAlreadyResolved,
    HumanRequestTypeMismatch,
    InvalidRunTransition,
    RunNotFound,
    SubmissionApprovalViolation,
)
from z_apply_core.integrations.models import (
    BrowserControlMode,
    BrowserTabState,
    CoreArtifact,
    CoreContextMessage,
    CoreEvent,
    CoreHumanRequest,
    CoreIntegrationConfig,
    CoreLiveView,
    CoreRunResult,
    CoreRunView,
    RunOutcome,
    RunPhase,
    RunStatus,
    StartRunRequest,
    utc_now,
)
from z_apply_core.memory.applicant_memory import CandidateMemory
from z_apply_core.runtime import RunResources, RunRuntime
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

DEFAULT_TASK = (
    "Complete the job application carefully, ask only for unavailable candidate facts, "
    "verify the review, and require human approval before final submission."
)

_EPHEMERAL_FRAMEWORK_EVENTS = frozenset(
    {
        "agent.message.delta",
        "model.tool_call.delta",
        "tool.progress",
    }
)


class _Run:
    def __init__(self, request: StartRunRequest, run_id: str) -> None:
        self.request = request
        self.run_id = run_id
        self.resources = RunResources()
        self.sequence = 0
        self.view = CoreRunView(
            run_id=run_id,
            job_url=request.job_url,
            task=request.task or DEFAULT_TASK,
            company=None,
            role=None,
            status=RunStatus.QUEUED,
            phase=RunPhase.QUEUED,
            outcome=None,
            summary=None,
            current_agent=None,
            current_model=None,
            browser_tab_state=BrowserTabState.PENDING,
            control_mode=BrowserControlMode.AGENT_CONTROL,
            pending_human_request_id=None,
            latest_event_sequence=0,
            created_at=utc_now(),
            started_at=None,
            finished_at=None,
        )
        self.task: asyncio.Task[None] | None = None
        self.done: asyncio.Future[CoreRunResult] | None = None
        self.human_requests: dict[str, CoreHumanRequest] = {}
        self.artifacts: list[CoreArtifact] = []
        self.context_inbox = ContextInbox()
        self.human_broker: HumanRequestBroker | None = None


class _GraphSink(FrameworkEventSink):
    def __init__(self, service: ZApplyCore, run: _Run) -> None:
        self._service = service
        self._run = run
        self._last_graph_payload: dict[str, Any] | None = None
        self._model_by_agent: dict[str, str] = {}

    async def accept(self, event: FrameworkTraceEvent) -> None:
        name = _public_agent_name(event.name or event.event)
        payload = _framework_payload(event)
        event_type = _typed_framework_event(event.event, payload)
        if event_type == "graph.event" and event.event not in {"values", "updates", "error"}:
            return
        if event_type == "graph.event":
            if not payload or payload == self._last_graph_payload:
                return
            self._last_graph_payload = payload
        if event_type in _EPHEMERAL_FRAMEWORK_EVENTS:
            return
        if name == "orchestrator" and self._run.view.phase is RunPhase.AUTHENTICATION:
            self._run.view = replace(self._run.view, phase=RunPhase.APPLICATION)
            await self._service._emit(
                self._run,
                "run.phase_changed",
                {"phase": RunPhase.APPLICATION.value},
            )
        if event_type in {
            "agent.started",
            "tool.started",
            "authentication.evidence",
        }:
            self._run.view = replace(self._run.view, current_agent=name)
        model = payload.get("model_id") or payload.get("auth_model_id")
        if isinstance(model, str):
            self._run.view = replace(self._run.view, current_model=model)
            if event_type == "model.selected":
                self._model_by_agent[name] = model
        if event_type.startswith("tool."):
            causal_model = self._model_by_agent.get(name)
            if causal_model is not None:
                payload = {**payload, "model_id": causal_model}
        await self._service._emit(
            self._run, event_type, payload, source={"component": "graph", "agent": name}
        )


def _public_agent_name(name: str) -> str:
    """Remove DeepAgents invocation identifiers from the public run contract."""
    return name.split(":", 1)[0]


class CoreRunHandle:
    """Capability object for a single run; no internal runtime leaks through it."""

    def __init__(self, service: ZApplyCore, run_id: str) -> None:
        self._service = service
        self._run_id = run_id

    @property
    def run_id(self) -> str:
        return self._run_id

    def _run(self) -> _Run:
        return self._service._require_run(self._run_id)

    async def view(self) -> CoreRunView:
        return self._run().view

    async def wait(self) -> CoreRunResult:
        run = self._run()
        if run.done is None:
            raise InvalidRunTransition("run has not been scheduled")
        return await asyncio.shield(run.done)

    async def cancel(self) -> CoreRunView:
        return await self._service._cancel(self._run())

    async def answer_human_request(
        self, request_id: str, answer: str, *, responder: str
    ) -> CoreHumanRequest:
        return await self._service._resolve_human(
            self._run(), request_id, answer=answer, responder=responder
        )

    async def decide_submission(
        self, request_id: str, approved: bool, *, responder: str
    ) -> CoreHumanRequest:
        return await self._service._resolve_submission(
            self._run(), request_id, approved=approved, responder=responder
        )

    async def focus_browser(self) -> CoreRunView:
        return await self._service.focus_run(self._run_id)

    async def take_browser_control(self) -> CoreRunView:
        return await self._service.take_browser_control(self._run_id)

    async def return_browser_control(self) -> CoreRunView:
        return await self._service.return_browser_control(self._run_id)

    async def close_browser(self) -> CoreRunView:
        return await self._service.close_run_browser(self._run_id)

    async def artifacts(self) -> tuple[CoreArtifact, ...]:
        return tuple(self._run().artifacts)

    async def human_requests(self) -> tuple[CoreHumanRequest, ...]:
        return tuple(self._run().human_requests.values())

    async def send_context(self, content: str, *, source: str) -> CoreContextMessage:
        return await self._service.send_context(self._run_id, content, source=source)


class ZApplyCore:
    def __init__(self, config: CoreIntegrationConfig | None = None) -> None:
        configured_limit = int(os.getenv("Z_APPLY_MAX_ACTIVE_RUNS", "3"))
        self._config = config or CoreIntegrationConfig(max_active_runs=configured_limit)
        self._runs: dict[str, _Run] = {}
        self._router: Any | None = None
        self._started = False
        self._closing = False
        self._scheduler: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._state_lock = asyncio.Lock()
        self._browser_lock = asyncio.Lock()
        self._broadcaster = EventBroadcaster()
        self._sinks: list[CoreEventSink] = []
        self._focused_run_id: str | None = None
        self._control_run_id: str | None = None
        self._workspace = BrowserWorkspace()
        self._candidate_memory: CandidateMemory | None = None
        self._telegram: Any | None = None

    async def start(self) -> None:
        if self._started:
            return
        if self._closing:
            raise CoreShuttingDown()
        self._router = make_router()
        self._candidate_memory = CandidateMemory()
        self._telegram = make_configured_human_channel()
        self._started = True
        self._scheduler = asyncio.create_task(self._schedule(), name="z-apply-core-scheduler")

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._wake.set()
        for run in tuple(self._runs.values()):
            with contextlib.suppress(Exception):
                await self._cancel(run)
        active_tasks = [run.task for run in self._runs.values() if run.task is not None]
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        if self._scheduler is not None:
            self._scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler
        with contextlib.suppress(Exception):
            await self._workspace.close()
        if self._telegram is not None:
            stop = getattr(self._telegram, "stop", None)
            if callable(stop):
                with contextlib.suppress(Exception):
                    await stop()
            self._telegram = None
        if self._candidate_memory is not None:
            with contextlib.suppress(Exception):
                self._candidate_memory.close()
            self._candidate_memory = None
        self._started = False

    def add_event_sink(self, sink: CoreEventSink) -> None:
        self._sinks.append(sink)

    async def start_run(
        self, request: StartRunRequest, *, run_id: str | None = None
    ) -> CoreRunHandle:
        if not self._started:
            await self.start()
        if self._closing:
            raise CoreShuttingDown()
        if not request.job_url.startswith(("https://", "http://")):
            raise ValueError("job_url must be an absolute HTTP(S) URL")
        resolved_id = run_id or str(uuid4())
        async with self._state_lock:
            if resolved_id in self._runs:
                raise InvalidRunTransition("run_id already exists")
            run = _Run(request, resolved_id)
            run.done = asyncio.get_running_loop().create_future()
            self._runs[resolved_id] = run
        await self._emit(run, "run.queued", {"job_url": request.job_url})
        self._wake.set()
        return CoreRunHandle(self, resolved_id)

    def get_run(self, run_id: str) -> CoreRunHandle | None:
        return CoreRunHandle(self, run_id) if run_id in self._runs else None

    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(
            run.run_id for run in self._runs.values() if run.view.status is not RunStatus.TERMINAL
        )

    async def focus_run(self, run_id: str) -> CoreRunView:
        run = self._require_run(run_id)
        if run.view.browser_tab_state is not BrowserTabState.OPEN:
            raise BrowserUnavailable()
        async with self._browser_lock:
            await self._workspace.focus(run_id)
            self._focused_run_id = run_id
        await self._emit(run, "browser.page_focused", {})
        return run.view

    async def take_browser_control(self, run_id: str) -> CoreRunView:
        run = self._require_run(run_id)
        if run.view.browser_tab_state is not BrowserTabState.OPEN:
            raise BrowserUnavailable()
        if self._control_run_id not in (None, run_id):
            raise BrowserControlConflict()
        try:
            await self._workspace.take_human_control(run_id)
        except RuntimeError as exc:
            raise BrowserControlConflict() from exc
        self._focused_run_id = run_id
        self._control_run_id = run_id
        run.view = replace(
            run.view, status=RunStatus.HUMAN_CONTROL, control_mode=BrowserControlMode.HUMAN_CONTROL
        )
        await self._emit(run, "browser.control_taken", {})
        return run.view

    async def return_browser_control(self, run_id: str) -> CoreRunView:
        run = self._require_run(run_id)
        if self._control_run_id != run_id:
            raise BrowserControlConflict()
        evidence = await self._workspace.return_agent_control(run_id)
        self._focused_run_id = run_id
        self._control_run_id = None
        next_status = (
            RunStatus.WAITING_HUMAN
            if run.view.pending_human_request_id is not None
            else RunStatus.RUNNING
        )
        run.view = replace(
            run.view, status=next_status, control_mode=BrowserControlMode.AGENT_CONTROL
        )
        await self._emit(run, "browser.snapshot_refreshed", {"excerpt": evidence[:500]})
        await self._emit(run, "browser.control_returned", {})
        return run.view

    async def close_run_browser(self, run_id: str) -> CoreRunView:
        run = self._require_run(run_id)
        if run.view.status is not RunStatus.TERMINAL:
            raise InvalidRunTransition("only retained terminal run pages may be closed")
        await self._workspace.close_run(run_id)
        run.view = replace(run.view, browser_tab_state=BrowserTabState.CLOSED)
        await self._emit(run, "browser.page_closed", {})
        return run.view

    async def live_view(self) -> CoreLiveView:
        if self._workspace.live_view.port is not None:
            return CoreLiveView(
                True,
                "127.0.0.1",
                self._workspace.live_view.port,
                BrowserControlMode.HUMAN_CONTROL
                if self._control_run_id is not None
                else BrowserControlMode.AGENT_CONTROL,
                self._focused_run_id,
            )
        return CoreLiveView(
            False, None, None, BrowserControlMode.AGENT_CONTROL, self._focused_run_id
        )

    async def send_context(
        self,
        run_id: str,
        content: str,
        *,
        source: str,
    ) -> CoreContextMessage:
        run = self._require_run(run_id)
        if run.view.status is RunStatus.TERMINAL:
            raise InvalidRunTransition("terminal runs cannot receive new context")
        normalized = content.strip()
        if not normalized:
            raise ValueError("context content must not be empty")
        if len(normalized) > 8_000:
            raise ValueError("context content must not exceed 8000 characters")
        accepted = CoreContextMessage(run_id, normalized, source, utc_now())
        run.context_inbox.put(ContextMessage(normalized, source))
        await self._emit(
            run,
            "context.received",
            {"source": source, "content": normalized},
            source={"component": "human", "agent": source},
        )
        return accepted

    async def shutdown_browser_workspace(self, *, force: bool = False) -> None:
        active = self.active_run_ids()
        if active and not force:
            raise InvalidRunTransition("cannot close browser workspace while applications execute")
        await self._workspace.close()
        for run in self._runs.values():
            run.view = replace(run.view, browser_tab_state=BrowserTabState.CLOSED)

    async def subscribe(self, *, run_id: str | None = None) -> AsyncIterator[CoreEvent]:
        async with self._broadcaster.subscription() as stream:
            async for event in stream:
                if run_id is None or event.run_id == run_id:
                    yield event

    async def _schedule(self) -> None:
        while not self._closing:
            runnable = [run for run in self._runs.values() if run.view.status is RunStatus.QUEUED]
            executing = sum(
                run.task is not None and not run.task.done() for run in self._runs.values()
            )
            for run in runnable[: max(0, self._config.max_active_runs - executing)]:
                run.task = asyncio.create_task(self._execute(run), name=f"z-apply-run-{run.run_id}")
                executing += 1
            self._wake.clear()
            await self._wake.wait()

    async def _execute(self, run: _Run) -> None:
        run.view = replace(
            run.view, status=RunStatus.STARTING, phase=RunPhase.SETUP, started_at=utc_now()
        )
        await self._emit(run, "run.started", {})
        try:
            lease = await self._workspace.open_run(run.run_id)
            self._focused_run_id = run.run_id
            run.view = replace(run.view, browser_tab_state=BrowserTabState.OPEN)
            await self._emit(run, "browser.page_opened", {})
            try:
                async with asyncio.timeout(self._config.browser_navigation_timeout_seconds):
                    await lease.session.call_tool("browser_navigate", {"url": run.request.job_url})
            except TimeoutError as exc:
                raise BrowserUnavailable(
                    "initial browser navigation exceeded the configured timeout"
                ) from exc
            run.human_broker = HumanRequestBroker(
                run_id=run.run_id,
                on_requested=lambda request: self._human_requested(run, request),
                on_resolved=lambda request: self._human_resolved(run, request),
                telegram=self._telegram,
            )
            runtime = RunRuntime(
                display=self._workspace.display,
                live_view=self._workspace.live_view,
                browser=lease.session,
                human_channel=BrokeredHumanChannel(run.human_broker, url=run.request.job_url),
                candidate_memory=self._candidate_memory,
                run_id=run.run_id,
                context_inbox=run.context_inbox,
                shared_resources=True,
                artifact_callback=lambda kind, path: self._record_artifact(run, kind, path),
            )
            run.resources.runtime = runtime
            run.view = replace(
                run.view,
                status=RunStatus.RUNNING,
                phase=RunPhase.AUTHENTICATION,
            )
            await self._emit(run, "run.phase_changed", {"phase": RunPhase.AUTHENTICATION.value})
            state, result = await run_job(
                run.request.job_url,
                task=run.request.task or DEFAULT_TASK,
                live_view=run.request.live_view,
                sink=_GraphSink(self, run),
                router=self._router,
                resources=run.resources,
                cleanup_resources=False,
                context_inbox=run.context_inbox,
                prepared_runtime=runtime,
            )
            status = str(state.get("run_status", "failed"))
            outcome = {
                "completed": RunOutcome.SUBMITTED_VERIFIED,
                "incomplete": RunOutcome.BLOCKED,
                "rejected": RunOutcome.REJECTED,
                "failed": RunOutcome.FAILED,
            }.get(status, RunOutcome.FAILED)
            summary = str(state.get("orchestrator_summary") or state.get("auth_summary") or status)
            browser_state = (
                BrowserTabState.CLOSED
                if outcome is RunOutcome.SUBMITTED_VERIFIED
                else BrowserTabState.OPEN
            )
            run.view = replace(
                run.view,
                status=RunStatus.TERMINAL,
                phase=RunPhase.TERMINAL,
                outcome=outcome,
                summary=summary,
                finished_at=utc_now(),
                browser_tab_state=browser_state,
            )
            if outcome is RunOutcome.SUBMITTED_VERIFIED:
                await self._workspace.close_run(run.run_id)
            await self._emit(run, "run.terminal", {"outcome": outcome, "summary": summary})
            assert run.done is not None
            run.done.set_result(
                CoreRunResult(
                    run.run_id,
                    outcome,
                    summary,
                    run.view.finished_at or utc_now(),
                    result.event_count,
                )
            )
        except asyncio.CancelledError:
            await self._terminal(run, RunOutcome.CANCELLED, "Run cancelled.")
            raise
        except Exception as exc:
            await self._terminal(
                run, RunOutcome.FAILED, "Core execution failed.", {"error": type(exc).__name__}
            )
        finally:
            if run.human_broker is not None:
                await run.human_broker.close()
            self._wake.set()

    async def _terminal(
        self, run: _Run, outcome: RunOutcome, summary: str, payload: dict[str, Any] | None = None
    ) -> None:
        run.view = replace(
            run.view,
            status=RunStatus.TERMINAL,
            phase=RunPhase.TERMINAL,
            outcome=outcome,
            summary=summary,
            finished_at=utc_now(),
        )
        await self._emit(
            run,
            "run.terminal",
            {"outcome": outcome, "summary": summary, **(payload or {})},
            level="error" if outcome is RunOutcome.FAILED else "info",
        )
        if run.done is not None and not run.done.done():
            run.done.set_result(
                CoreRunResult(
                    run.run_id, outcome, summary, run.view.finished_at or utc_now(), run.sequence
                )
            )

    async def _cancel(self, run: _Run) -> CoreRunView:
        if run.view.status is RunStatus.TERMINAL:
            raise InvalidRunTransition("terminal runs cannot be cancelled")
        await self._emit(run, "run.cancel_requested", {})
        if run.task is not None:
            run.task.cancel()
        else:
            await self._terminal(run, RunOutcome.CANCELLED, "Run cancelled before execution.")
        return run.view

    async def _resolve_human(
        self, run: _Run, request_id: str, *, answer: str, responder: str
    ) -> CoreHumanRequest:
        request = run.human_requests.get(request_id)
        if request is None:
            raise RunNotFound()
        if request.kind == "submission_approval":
            raise HumanRequestTypeMismatch()
        if request.status != "pending":
            raise HumanRequestAlreadyResolved()
        if run.human_broker is None:
            raise InvalidRunTransition("run has no active human request broker")
        try:
            await run.human_broker.resolve_answer(request_id, answer, responder=responder)
        except KeyError as exc:
            raise HumanRequestAlreadyResolved() from exc
        return run.human_requests[request_id]

    async def _resolve_submission(
        self, run: _Run, request_id: str, *, approved: bool, responder: str
    ) -> CoreHumanRequest:
        request = run.human_requests.get(request_id)
        if request is None:
            raise RunNotFound()
        if request.kind != "submission_approval":
            raise HumanRequestTypeMismatch()
        if request.status != "pending":
            raise HumanRequestAlreadyResolved()
        if run.view.phase is not RunPhase.APPROVAL:
            raise SubmissionApprovalViolation()
        if run.human_broker is None:
            raise InvalidRunTransition("run has no active human request broker")
        try:
            await run.human_broker.resolve_submission(request_id, approved, responder=responder)
        except KeyError as exc:
            raise HumanRequestAlreadyResolved() from exc
        return run.human_requests[request_id]

    async def _human_requested(self, run: _Run, request: BrokerRequest) -> None:
        image_artifact_id: str | None = None
        if request.image_path:
            artifact = await self._artifact_created(
                run, "human_challenge", Path(request.image_path)
            )
            image_artifact_id = artifact.artifact_id
        public = _public_human_request(request, image_artifact_id=image_artifact_id)
        run.human_requests[public.request_id] = public
        phase = RunPhase.APPROVAL if public.kind == "submission_approval" else run.view.phase
        run.view = replace(
            run.view,
            status=RunStatus.WAITING_HUMAN,
            phase=phase,
            pending_human_request_id=public.request_id,
        )
        await self._emit(
            run,
            "submission.approval_requested"
            if public.kind == "submission_approval"
            else "human.requested",
            {
                "request_id": public.request_id,
                "kind": public.kind,
                "question": public.question,
                "context": public.context,
                "options": list(public.options),
                "risk": public.risk,
                "allow_free_text": public.allow_free_text,
                "image_artifact_id": public.image_artifact_id,
                "created_at": public.created_at.isoformat(),
            },
        )

    async def _human_resolved(self, run: _Run, request: BrokerRequest) -> None:
        existing = run.human_requests.get(request.request_id)
        public = _public_human_request(
            request,
            image_artifact_id=(existing.image_artifact_id if existing is not None else None),
        )
        run.human_requests[public.request_id] = public
        next_status = (
            RunStatus.HUMAN_CONTROL
            if run.view.control_mode is BrowserControlMode.HUMAN_CONTROL
            else RunStatus.RUNNING
        )
        run.view = replace(
            run.view,
            status=next_status,
            pending_human_request_id=None,
        )
        if public.status == "cancelled":
            event_type = "human.cancelled"
        elif public.kind == "submission_approval":
            event_type = "submission.approved" if public.approved else "submission.rejected"
        else:
            event_type = "human.resolved"
        await self._emit(
            run,
            event_type,
            {
                "request_id": public.request_id,
                "kind": public.kind,
                "status": public.status,
                "answer": public.answer,
                "approved": public.approved,
                "responder": public.responder,
                "resolved_at": (
                    public.resolved_at.isoformat() if public.resolved_at is not None else None
                ),
            },
        )

    async def _artifact_created(self, run: _Run, kind: str, path: Any) -> CoreArtifact:
        resolved = path.resolve()
        run_artifact_root = (ARTIFACT_ROOT / run.run_id).resolve()
        if not resolved.is_file() or not resolved.is_relative_to(run_artifact_root):
            raise ValueError("artifact path is outside the run artifact root")
        content = await asyncio.to_thread(resolved.read_bytes)
        artifact = CoreArtifact(
            artifact_id=str(uuid4()),
            run_id=run.run_id,
            kind=kind,
            filename=resolved.name,
            mime_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
            relative_path=resolved.relative_to(ARTIFACT_ROOT.resolve()).as_posix(),
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=utc_now(),
        )
        run.artifacts.append(artifact)
        await self._emit(
            run,
            "artifact.created",
            {
                "artifact_id": artifact.artifact_id,
                "kind": artifact.kind,
                "filename": artifact.filename,
                "mime_type": artifact.mime_type,
                "relative_path": artifact.relative_path,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
                "created_at": artifact.created_at.isoformat(),
            },
        )
        return artifact

    async def _record_artifact(self, run: _Run, kind: str, path: Any) -> None:
        await self._artifact_created(run, kind, path)

    def _require_run(self, run_id: str) -> _Run:
        run = self._runs.get(run_id)
        if run is None:
            raise RunNotFound()
        return run

    async def _emit(
        self,
        run: _Run,
        event_type: str,
        payload: dict[str, Any],
        *,
        source: dict[str, str] | None = None,
        level: str = "info",
    ) -> None:
        run.sequence += 1
        run.view = replace(run.view, latest_event_sequence=run.sequence)
        event = CoreEvent(
            run.run_id,
            run.sequence,
            utc_now(),
            event_type,
            source or {"component": "core"},
            level,
            _safe_payload(payload),
        )
        for sink in tuple(self._sinks):
            await sink.accept(event)
        await self._broadcaster.publish(event)


def _public_human_request(
    request: BrokerRequest, *, image_artifact_id: str | None = None
) -> CoreHumanRequest:
    return CoreHumanRequest(
        request_id=request.request_id,
        run_id=request.run_id,
        kind=request.kind,
        question=request.question,
        context=request.context,
        options=request.options,
        risk=request.risk,
        allow_free_text=request.allow_free_text,
        image_artifact_id=image_artifact_id,
        created_at=request.created_at,
        status=request.status,
        answer=request.answer,
        approved=request.approved,
        responder=request.responder,
        resolved_at=request.resolved_at,
    )


def _typed_framework_event(event: str, payload: dict[str, Any]) -> str:
    if event == "agent_lifecycle":
        return {
            "started": "agent.started",
            "running": "agent.started",
            "completed": "agent.completed",
            "failed": "agent.failed",
        }.get(str(payload.get("status", "")), "agent.changed")
    return {
        "agent_tool_start": "tool.started",
        "agent_tool_end": "tool.completed" if not payload.get("error") else "tool.failed",
        "agent_tool_delta": "tool.progress",
        "agent_message_delta": "agent.message.delta",
        "agent_model_tool_call": "model.tool_call.delta",
        "model_selected": "model.selected",
        "model_failed": "model.failed",
        "model_rotated": "model.rotated",
        "model_rate_limited": "model.rate_limited",
        "recovery_started": "recovery.started",
        "recovery_completed": "recovery.completed",
        "recovery_exhausted": "recovery.exhausted",
        "authentication_evidence": "authentication.evidence",
        "auth": "authentication.evidence",
        "submission_readiness": (
            "submission.review_ready" if payload.get("ready") else "submission.review_not_ready"
        ),
    }.get(event, "graph.event")


def _safe_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"value": str(value)[:1000]}
    return {
        str(key): sanitized
        for key, item in value.items()
        if str(key).lower() not in _PRIVATE_PAYLOAD_KEYS
        if (sanitized := _safe_value(item)) is not None
    }


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, dict):
        return {
            str(key): sanitized
            for key, item in list(value.items())[:50]
            if str(key).lower() not in _PRIVATE_PAYLOAD_KEYS
            if (sanitized := _safe_value(item, depth=depth + 1)) is not None
        }
    if isinstance(value, (list, tuple)):
        return [
            sanitized
            for item in value[:50]
            if (sanitized := _safe_value(item, depth=depth + 1)) is not None
        ]
    return None


_PRIVATE_PAYLOAD_KEYS = frozenset(
    {
        "authorization",
        "browser_tools",
        "candidate_memory",
        "human_channel",
        "messages",
        "password",
        "raw",
        "resources",
        "runtime",
        "token",
        "verification_url",
    }
)

_PUBLIC_STATE_FIELDS = frozenset(
    {
        "auth_model_id",
        "auth_status",
        "auth_summary",
        "model_id",
        "orchestrator_summary",
        "run_status",
    }
)


def _framework_payload(event: FrameworkTraceEvent) -> dict[str, Any]:
    """Normalize framework state events without exposing live graph objects."""
    if event.event not in {"values", "updates"}:
        return _safe_payload(event.data)
    data = event.data.get("data")
    if not isinstance(data, dict):
        return {}
    if event.event == "updates":
        values = [item for item in data.values() if isinstance(item, dict)]
        data = {key: value for item in values for key, value in item.items()}
    return _safe_payload({key: data[key] for key in _PUBLIC_STATE_FIELDS if key in data})
