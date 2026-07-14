from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from playwright_python_mcp.mcp import create_connection

from z_apply_core.browser_config import build_browser_config
from z_apply_core.browser_session import ARTIFACT_ROOT, BrowserSession, BrowserToolExecutionError
from z_apply_core.live_view import LiveView
from z_apply_core.virtual_display import VirtualDisplaySession


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
                lambda: not self._human_control
                and not self._takeover_pending
                and not self._operation_active
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
    """Run-scoped browser capability over one keyed MCP backend."""

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
                    await page.close()
        self.owned_pages.clear()

    async def stop_loading(self) -> None:
        """Leave retained pages inspectable without background navigation work."""
        for page in tuple(self.owned_pages):
            if page.is_closed():
                continue
            with contextlib.suppress(Exception):
                await page.evaluate("window.stop()")


class BrowserWorkspace:
    """One persistent Camoufox workspace shared by all application runs."""

    def __init__(self) -> None:
        self.display = VirtualDisplaySession(enabled=True)
        self.live_view = LiveView()
        self.gate = BrowserControlGate()
        self._server: Any | None = None
        self._anchor_backend: Any | None = None
        self._context: Any | None = None
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
                config = build_browser_config("workspace")
                config["sharedBrowserContext"] = True
                self._server = await create_connection(config)
                self._anchor_backend = await self._server.backend_pool.backend_for(
                    "__z_apply_workspace__"
                )
                self._context = await self._anchor_backend._ensure_context(
                    cwd=Path.cwd(), roots=None
                )
                await self._normalize_restored_pages()
                self._started = True
            except Exception:
                self._server = None
                self._anchor_backend = None
                self._context = None
                self.live_view.stop()
                self.display.stop()
                raise

    async def open_run(self, run_id: str) -> RunBrowserLease:
        await self.start()
        assert self._server is not None
        assert self._anchor_backend is not None
        assert self._context is not None
        async with self._creation_lock:
            if run_id in self._leases:
                raise RuntimeError(f"browser lease already exists for run {run_id}")
            async with self.gate.mutation():
                artifact_dir = (ARTIFACT_ROOT / run_id / "browser-artifacts").resolve()
                artifact_dir.mkdir(parents=True, exist_ok=True)
                backend = self._anchor_backend
                context = self._context
                tab = await context.new_tab()
            session = BrowserSession.from_backend(
                backend,
                tools=tuple(self._server.backend_pool.tools),
                run_id=run_id,
                mutation_gate=self.gate,
            )
            lease = RunBrowserLease(
                run_id=run_id,
                backend=backend,
                context=context,
                primary_tab=tab,
                session=session,
            )
            session.bind_lease(lease)
            self._leases[run_id] = lease
        return lease

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
        evidence = await lease.session.call_tool("browser_snapshot")
        await self.gate.release()
        return evidence

    async def close_run(self, run_id: str) -> None:
        lease = self._leases.pop(run_id, None)
        if lease is None:
            return
        async with self.gate.mutation():
            await lease.close_pages()

    async def quiesce_run(self, run_id: str) -> None:
        """Stop retained run pages from consuming shared browser resources."""
        lease = self._leases.get(run_id)
        if lease is None or lease.closed:
            return
        async with self.gate.mutation():
            await lease.stop_loading()

    async def close(self) -> None:
        for run_id in tuple(self._leases):
            await self.close_run(run_id)
        if self._server is not None:
            await self._server.backend_pool.close_all()
        self._server = None
        self._anchor_backend = None
        self._context = None
        self._started = False
        self.live_view.stop()
        self.display.stop()

    def _require_lease(self, run_id: str) -> RunBrowserLease:
        lease = self._leases.get(run_id)
        if lease is None or lease.closed:
            raise BrowserToolExecutionError("The run's browser page is unavailable.")
        return lease

    async def _normalize_restored_pages(self) -> None:
        """Retain one blank Camoufox window and discard stale restored run pages."""
        assert self._context is not None
        tabs = tuple(self._context.tabs())
        if not tabs:
            return
        anchor, *restored = tabs
        with contextlib.suppress(Exception):
            await anchor.page.goto("about:blank", wait_until="commit", timeout=5_000)
        for tab in restored:
            with contextlib.suppress(Exception):
                if not tab.page.is_closed():
                    await tab.page.close()
