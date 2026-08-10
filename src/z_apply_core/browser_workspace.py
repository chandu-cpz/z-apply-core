from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from z_apply_core.browser_pool import BrowserPool
from z_apply_core.browser_session import BrowserSession, BrowserToolExecutionError
from z_apply_core.live_view import LiveView
from z_apply_core.virtual_display import VirtualDisplaySession

logger = logging.getLogger(__name__)

_RETURN_CONTROL_SNAPSHOT_TIMEOUT_SECONDS = 15.0

DEFAULT_MAX_ACTIVE_RUNS = 3


class BrowserControlGate:
    """Workspace-global exclusion gate for browser mutations and VNC control."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._takeover_pending = False
        self._human_control = False
        self._operation_active = False

    @property
    def human_control(self) -> bool:
        return self._human_control

    @asynccontextmanager
    async def mutation(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: (
                    not self._human_control
                    and not self._takeover_pending
                    and not self._operation_active
                )
            )
            self._operation_active = True
        try:
            yield
        finally:
            async with self._condition:
                self._operation_active = False
                self._condition.notify_all()

    async def take(self) -> None:
        async with self._condition:
            if self._human_control or self._takeover_pending:
                raise RuntimeError("browser workspace is already under human control")
            self._takeover_pending = True
            await self._condition.wait_for(lambda: not self._operation_active)
            self._human_control = True
            self._takeover_pending = False

    async def release(self) -> None:
        async with self._condition:
            if not self._human_control:
                raise RuntimeError("browser workspace is not under human control")
            self._human_control = False
            self._condition.notify_all()


class RunBrowserLease:
    """Run-scoped browser capability over one dedicated per-run instance."""

    def __init__(
        self,
        *,
        run_id: str,
        backend: Any,
        context: Any,
        primary_tab: Any,
        session: BrowserSession,
    ) -> None:
        self.run_id = run_id
        self.backend = backend
        self.context = context
        self.primary_tab = primary_tab
        self.session = session
        self.owned_pages: set[Any] = {primary_tab.page}
        self.closed = False

    def owns_current_page(self) -> bool:
        tab = self.context.current_tab()
        return tab is not None and tab.page in self.owned_pages

    async def focus(self) -> None:
        tab = self.context.current_tab()
        if tab is None or tab.page not in self.owned_pages:
            tab = self.primary_tab if self.primary_tab.page in self.owned_pages else None
        if tab is None or tab.page.is_closed():
            raise BrowserToolExecutionError("The run's browser page is no longer available.")
        await self.context.select_tab(self.context.tabs().index(tab))

    async def discover_owned_popups(self) -> None:
        for tab in self.context.tabs():
            page = tab.page
            if page in self.owned_pages or page.is_closed():
                continue
            opener = await page.opener()
            if opener in self.owned_pages:
                self.owned_pages.add(page)

    async def close_pages(self) -> None:
        if self.closed:
            return
        self.closed = True
        for page in tuple(self.owned_pages):
            with contextlib.suppress(Exception):
                if not page.is_closed():
                    # A page mid-navigation (hung network, stuck load) can make
                    # playwright's close() block forever, hanging the whole
                    # backend shutdown. Bound each close so teardown always
                    # completes and the pool can reset the slot.
                    try:
                        await asyncio.wait_for(page.close(), timeout=5)
                    except TimeoutError:
                        logger.warning("page.close() timed out; continuing shutdown")
        self.owned_pages.clear()

    async def stop_loading(self) -> None:
        """Leave retained pages inspectable without background navigation work."""
        for page in tuple(self.owned_pages):
            if page.is_closed():
                continue
            with contextlib.suppress(Exception):
                await page.keyboard.press("Escape")


class BrowserWorkspace:
    """Per-run dedicated Camoufox instances leased from the browser pool.

    Replaces the old single shared browser (one process + one shared profile,
    tabs per run): every run now leases a disjoint profile slot and the pool
    launches a DEDICATED instance on it. A crash or hang on one run cannot
    affect the others, and no two runs ever share a profile directory (the
    Firefox single-writer lock that previously caused launch hangs).
    """

    def __init__(self, *, pool: BrowserPool | None = None) -> None:
        self.display = VirtualDisplaySession(enabled=True)
        self.live_view = LiveView()
        self.gate = BrowserControlGate()
        self._pool = pool or BrowserPool(max_active=DEFAULT_MAX_ACTIVE_RUNS)
        self._leases: dict[str, RunBrowserLease] = {}
        self._start_lock = asyncio.Lock()
        self._creation_lock = asyncio.Lock()
        self._started = False

    async def start(self, *, live_view: bool = True) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            self.display.start()
            try:
                self.live_view.start(self.display.display, enabled=live_view, open_client=False)
                # Provision slots (mirror from the sealed master, reconcile
                # orphaned leases, reap zombies) — no browser is launched until
                # a run asks for one.
                await self._pool.provision()
                self._started = True
            except Exception:
                self.live_view.stop()
                self.display.stop()
                raise

    async def open_run(self, run_id: str) -> RunBrowserLease:
        await self.start()
        assert self._started
        async with self._creation_lock:
            if run_id in self._leases:
                raise RuntimeError(f"browser lease already exists for run {run_id}")
            # Leases a slot and launches a dedicated instance (admission
            # control, bounded launch, health probe all live in the pool).
            pool_lease = await self._pool.acquire(run_id)
            try:
                async with self.gate.mutation():
                    context = pool_lease.context
                    tabs = tuple(context.tabs())
                    # The dedicated instance opens with one blank tab; reuse it
                    # rather than stacking a second tab on top.
                    tab = tabs[0] if tabs else await context.new_tab()
                session = BrowserSession.from_backend(
                    pool_lease.backend,
                    tools=tuple(pool_lease.server.backend_pool.tools),
                    run_id=run_id,
                    mutation_gate=self.gate,
                )
                lease = RunBrowserLease(
                    run_id=run_id,
                    backend=pool_lease.backend,
                    context=context,
                    primary_tab=tab,
                    session=session,
                )
                session.bind_lease(lease)
                self._leases[run_id] = lease
                return lease
            except Exception:
                # Never leak the pool lease if session construction fails.
                await self._pool.release(run_id, reason="open_run_failed")
                raise

    def lease(self, run_id: str) -> RunBrowserLease | None:
        return self._leases.get(run_id)

    async def focus(self, run_id: str) -> None:
        lease = self._require_lease(run_id)
        async with self.gate.mutation():
            await lease.focus()

    async def take_human_control(self, run_id: str) -> None:
        lease = self._require_lease(run_id)
        await self.gate.take()
        try:
            await lease.focus()
        except Exception:
            await self.gate.release()
            raise

    async def return_agent_control(self, run_id: str) -> str:
        lease = self._require_lease(run_id)
        await lease.focus()
        try:
            async with asyncio.timeout(_RETURN_CONTROL_SNAPSHOT_TIMEOUT_SECONDS):
                evidence = await lease.session.capture_control_return_evidence()
        except TimeoutError:
            await lease.stop_loading()
            try:
                async with asyncio.timeout(_RETURN_CONTROL_SNAPSHOT_TIMEOUT_SECONDS):
                    evidence = await lease.session.capture_control_return_evidence()
            except TimeoutError as exc:
                raise BrowserToolExecutionError(
                    "The browser did not produce fresh evidence after stopping a stalled "
                    "page load; human control remains active."
                ) from exc
        await self.gate.release()
        return evidence

    async def close_run(self, run_id: str) -> None:
        lease = self._leases.pop(run_id, None)
        if lease is None:
            return
        async with self.gate.mutation():
            await lease.close_pages()
        # Close the dedicated instance (bounded) and reset the slot from the
        # sealed master. No sync-back: the run's cookies/caches are discarded.
        await self._pool.release(run_id, reason="run_complete")

    async def quiesce_run(self, run_id: str) -> None:
        """Stop retained run pages from consuming browser resources."""
        lease = self._leases.get(run_id)
        if lease is None or lease.closed:
            return
        async with self.gate.mutation():
            await lease.stop_loading()

    async def close(self) -> None:
        for run_id in tuple(self._leases):
            await self.close_run(run_id)
        await self._pool.close()
        self._started = False
        self.live_view.stop()
        self.display.stop()

    def _require_lease(self, run_id: str) -> RunBrowserLease:
        lease = self._leases.get(run_id)
        if lease is None or lease.closed:
            raise BrowserToolExecutionError("The run's browser page is unavailable.")
        return lease
