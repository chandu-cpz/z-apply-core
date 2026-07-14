"""Long-lived Core service used by local transports.

This module deliberately converts graph trace events at the boundary.  It never
passes graph state, raw LangChain events, or browser objects to callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

from z_apply_core.graph import make_router, run_job
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
from z_apply_core.runtime import RunResources
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

DEFAULT_TASK = (
    "Complete the job application carefully, ask only for unavailable candidate facts, "
    "verify the review, and require human approval before final submission."
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
            task=request.task,
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


class _GraphSink(FrameworkEventSink):
    def __init__(self, service: ZApplyCore, run: _Run) -> None:
        self._service = service
        self._run = run

    async def accept(self, event: FrameworkTraceEvent) -> None:
        name = event.name or event.event
        payload = _safe_payload(event.data)
        event_type = _event_type(event.event, name)
        if event_type == "agent.started":
            self._run.view = replace(self._run.view, current_agent=name)
        model = payload.get("model_id")
        if isinstance(model, str):
            self._run.view = replace(self._run.view, current_model=model)
        await self._service._emit(
            self._run, event_type, payload, source={"component": "graph", "agent": name}
        )


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

    async def start(self) -> None:
        if self._started:
            return
        if self._closing:
            raise CoreShuttingDown()
        self._router = make_router()
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
        if self._scheduler is not None:
            self._scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler
        for run in self._runs.values():
            if run.resources.runtime is not None:
                with contextlib.suppress(Exception):
                    await run.resources.runtime.close()
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
        runtime = run.resources.runtime
        if runtime is None:
            raise BrowserUnavailable()
        async with self._browser_lock:
            backend = runtime.browser._backend
            tab = await backend._ensure_tab()
            await tab.page.bring_to_front()
            self._focused_run_id = run_id
        await self._emit(run, "browser.page_focused", {})
        return run.view

    async def take_browser_control(self, run_id: str) -> CoreRunView:
        run = self._require_run(run_id)
        if run.view.browser_tab_state is not BrowserTabState.OPEN:
            raise BrowserUnavailable()
        if self._control_run_id not in (None, run_id):
            raise BrowserControlConflict()
        await self.focus_run(run_id)
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
        runtime = run.resources.runtime
        if runtime is None:
            raise BrowserUnavailable()
        await self.focus_run(run_id)
        evidence = await runtime.browser.call_tool("browser_snapshot")
        self._control_run_id = None
        run.view = replace(
            run.view, status=RunStatus.RUNNING, control_mode=BrowserControlMode.AGENT_CONTROL
        )
        await self._emit(run, "browser.snapshot_refreshed", {"excerpt": evidence[:500]})
        await self._emit(run, "browser.control_returned", {})
        return run.view

    async def close_run_browser(self, run_id: str) -> CoreRunView:
        run = self._require_run(run_id)
        if run.view.status is not RunStatus.TERMINAL:
            raise InvalidRunTransition("only retained terminal run pages may be closed")
        if run.resources.runtime is not None:
            await run.resources.runtime.browser.close()
        run.view = replace(run.view, browser_tab_state=BrowserTabState.CLOSED)
        await self._emit(run, "browser.page_closed", {})
        return run.view

    async def live_view(self) -> CoreLiveView:
        for run in self._runs.values():
            runtime = run.resources.runtime
            if runtime is not None and runtime.live_view.port is not None:
                return CoreLiveView(
                    True,
                    "127.0.0.1",
                    runtime.live_view.port,
                    run.view.control_mode,
                    self._focused_run_id,
                )
        return CoreLiveView(
            False, None, None, BrowserControlMode.AGENT_CONTROL, self._focused_run_id
        )

    async def shutdown_browser_workspace(self, *, force: bool = False) -> None:
        active = self.active_run_ids()
        if active and not force:
            raise InvalidRunTransition("cannot close browser workspace while applications execute")
        for run in self._runs.values():
            if run.resources.runtime is not None:
                await run.resources.runtime.browser.close()
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
            state, result = await run_job(
                run.request.job_url,
                task=run.request.task or DEFAULT_TASK,
                live_view=run.request.live_view,
                sink=_GraphSink(self, run),
                router=self._router,
                resources=run.resources,
                cleanup_resources=False,
            )
            status = str(state.get("run_status", "failed"))
            outcome = RunOutcome.SUBMITTED_VERIFIED if status == "completed" else RunOutcome.BLOCKED
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
            if outcome is RunOutcome.SUBMITTED_VERIFIED and run.resources.runtime is not None:
                await run.resources.runtime.browser.close()
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
        resolved = replace(
            request, status="resolved", answer=answer, responder=responder, resolved_at=utc_now()
        )
        run.human_requests[request_id] = resolved
        await self._emit(run, "human.resolved", {"request_id": request_id, "responder": responder})
        return resolved

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
        resolved = replace(
            request,
            status="resolved",
            approved=approved,
            responder=responder,
            resolved_at=utc_now(),
        )
        run.human_requests[request_id] = resolved
        await self._emit(
            run,
            "submission.approved" if approved else "submission.rejected",
            {"request_id": request_id, "responder": responder},
        )
        return resolved

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


def _event_type(event: str, name: str) -> str:
    lower = f"{event} {name}".lower()
    if "model" in lower:
        return "model.selected"
    if "tool" in lower:
        return "tool.completed" if "end" in lower else "tool.started"
    if "error" in lower:
        return "agent.failed"
    return "agent.completed" if "end" in lower else "agent.started"


def _safe_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"value": str(value)[:1000]}
    allowed = {"name", "agent_path", "model_id", "run_id", "event_seq", "error", "output", "status"}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if str(key).lower() in {"raw", "messages", "token", "password", "authorization", "url"}:
            continue
        if key in allowed and isinstance(item, (str, int, float, bool, type(None))):
            result[str(key)] = str(item)[:1000] if isinstance(item, str) else item
    return result
