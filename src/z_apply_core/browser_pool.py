"""Operational browser pool: one dedicated Camoufox instance per run.

Phase 2 of the multi-browser pool. Builds on
``profile_pool.ProfileSlotPool``: each run leases a disjoint profile slot and
the pool launches a DEDICATED browser instance on it, so a crash or hang on
one run cannot affect another (crash isolation), and no two runs ever share a
profile directory (Firefox single-writer lock — the 180s launch-hang class).

Operational rules folded in from the ecosystem (Crawlee browser-pool,
Browserless, and the Camoufox issue tracker):

- **Admission control**: at most ``max_active`` simultaneous instances; beyond
  that, acquire fails fast (the caller decides to queue or reject).
- **Serialized acquisition**: launch selection is serialized with a lock
  (camoufox #553 — concurrent page/context creation can deadlock).
- **Bounded launch and close**: a stuck launch must never hang the pool
  (browser-pool ``operationTimeoutSecs``), and a wedged close must never
  block it either (camoufox #363 — raising close() leaked node drivers).
- **Protocol-level health probe**: a process can be alive while the protocol
  pipe is dead; verify the context responds before handing the browser out.
- **Retire-and-recycle**: browsers are never trusted — every release resets
  the slot from the sealed master (no sync-back) and the next run launches
  fresh.
- **Zombie reaping**: an orphaned slot (process died mid-lease) gets its
  leftover Firefox/node processes killed and the profile reset.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from z_apply_core.browser_config import build_browser_config
from z_apply_core.profile_pool import (
    DEFAULT_MASTER_PROFILE,
    DEFAULT_SLOT_COUNT,
    PROFILES_ROOT,
    ProfileSlot,
    ProfileSlotPool,
    _clean_launch_artifacts,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ACTIVE = 3
DEFAULT_LAUNCH_TIMEOUT_SECONDS = 45.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 15.0
LAUNCH_ATTEMPTS = 3

Launcher = Callable[[str, Path, str | None], Awaitable[tuple[Any, Any, Any]]]


class PoolCapacityError(RuntimeError):
    """The pool has no capacity for another concurrent browser instance."""


class BrowserLeaseError(RuntimeError):
    """A browser instance could not be launched or verified."""


@dataclass
class PoolStats:
    """Lightweight operational metrics for the pool (logged, not persisted)."""

    launches: int = 0
    launch_failures: int = 0
    launch_retries: int = 0
    capacity_rejections: int = 0
    capacity_waits: int = 0
    closes: int = 0
    close_failures: int = 0
    quarantine_count: int = 0
    max_active_seen: int = 0
    launch_durations: deque[float] = field(default_factory=lambda: deque(maxlen=64))
    retire_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def launch_p95(self) -> float:
        if not self.launch_durations:
            return 0.0
        ordered = sorted(self.launch_durations)
        idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[idx]

    def summary(self) -> dict[str, Any]:
        return {
            "launches": self.launches,
            "launch_failures": self.launch_failures,
            "launch_retries": self.launch_retries,
            "launch_p95_s": round(self.launch_p95, 2),
            "capacity_rejections": self.capacity_rejections,
            "closes": self.closes,
            "close_failures": self.close_failures,
            "quarantined": self.quarantine_count,
            "max_active_seen": self.max_active_seen,
            "retire_reasons": dict(self.retire_reasons),
        }


@dataclass
class BrowserLease:
    """A run's dedicated browser instance bound to one profile slot."""

    run_id: str
    slot: ProfileSlot
    server: Any
    backend: Any
    context: Any
    launched_at: float

    @property
    def profile_dir(self) -> Path:
        return self.slot.dir


def _kill_processes_on_profile(profile_dir: Path) -> None:
    """SIGTERM (then SIGKILL) every process referencing this profile dir.

    Matches only processes whose command line contains the slot path — a
    leftover Firefox process holding an orphaned slot would otherwise block
    the relaunch with the Firefox profile lock.
    """
    needle = str(profile_dir)
    try:
        result = subprocess.run(
            ["pgrep", "-f", needle], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return
    pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for pid in pids:
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(["kill", pid], capture_output=True, timeout=5)
    if pids:
        # Grace period for clean shutdown, then force-kill survivors.
        time.sleep(1.0)
        for pid in pids:
            with contextlib.suppress(subprocess.TimeoutExpired):
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
        logger.warning("killed %s process(es) referencing %s", len(pids), needle)


class BrowserPool:
    """Pool of per-run dedicated Camoufox instances on disjoint profile slots."""

    def __init__(
        self,
        *,
        master: Path = DEFAULT_MASTER_PROFILE,
        slots_root: Path = PROFILES_ROOT,
        slot_count: int = DEFAULT_SLOT_COUNT,
        max_active: int = DEFAULT_MAX_ACTIVE,
        launcher: Launcher | None = None,
        launch_timeout: float = DEFAULT_LAUNCH_TIMEOUT_SECONDS,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
        retry_backoff_base: float = 3.0,
    ) -> None:
        self._slots = ProfileSlotPool(
            master=master, slots_root=slots_root, slot_count=slot_count
        )
        self._max_active = max_active
        self._launcher = launcher or self._default_launcher
        self._launch_timeout = launch_timeout
        self._probe_timeout = probe_timeout
        self._close_timeout = close_timeout
        self._retry_backoff_base = retry_backoff_base
        self._leases: dict[str, BrowserLease] = {}
        self._lock = asyncio.Lock()
        self._release_event = asyncio.Event()
        self._stats = PoolStats()

    # -- lifecycle -----------------------------------------------------------

    async def provision(self) -> None:
        """Provision slots, reconcile orphaned leases, and reap zombies."""
        await self._slots.provision()
        for slot in self._slots.slots:
            await asyncio.to_thread(_kill_processes_on_profile, slot.dir)

    @property
    def stats(self) -> PoolStats:
        return self._stats

    @property
    def active_count(self) -> int:
        return len(self._leases)

    @property
    def capacity(self) -> int:
        return self._max_active

    def lease(self, run_id: str) -> BrowserLease | None:
        return self._leases.get(run_id)

    # -- acquire / release ---------------------------------------------------

    async def acquire(
        self, run_id: str, *, display: str | None = None, wait_timeout: float = 660.0
    ) -> BrowserLease:
        """Lease a slot and launch a dedicated instance for ``run_id``.

        ``display`` (optional) is the run's own X display; it is passed per-
        launch via env so concurrent runs each render on their own screen.
        ``wait_timeout`` bounds how long an at-capacity run waits for a slot
        before raising ``PoolCapacityError``; it defaults to slightly more
        than the backend's retained-browser TTL (``RETAINED_BROWSER_TTL_SECONDS``
        = 600s) so a run waiting on a retained browser acquires it when the
        TTL releases the slot instead of failing and being re-queued (which
        would just wait again).

        Raises ``PoolCapacityError`` when still at capacity after the wait,
        ``BrowserLeaseError`` when the browser could not be launched/verified
        after retries, and ``ProfileSlotError`` when no slot is available or
        reset fails.
        """
        deadline = time.monotonic() + wait_timeout
        while True:
            self._stats.max_active_seen = max(self._stats.max_active_seen, self.active_count)
            async with self._lock:
                if run_id in self._leases:
                    raise BrowserLeaseError(f"browser lease already exists for run {run_id}")
                if self.active_count < self._max_active:
                    slot = await self._slots.acquire(run_id)
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stats.capacity_rejections += 1
                raise PoolCapacityError(
                    f"pool at capacity ({self.active_count}/{self._max_active}) after "
                    f"{wait_timeout:.0f}s wait; cannot launch a browser for run {run_id}"
                )
            # A slot is held by a retained/terminal browser: wait for a release
            # signal (bounded, so a stuck pool still surfaces as capacity error).
            self._stats.capacity_waits += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._release_event.wait(), timeout=min(remaining, 20.0))
            self._release_event.clear()
        # Launch outside the lock: serialization covers selection, not the
        # (possibly slow) launch. Retry transient launch failures.
        lease: BrowserLease | None = None
        last_error: Exception | None = None
        for attempt in range(LAUNCH_ATTEMPTS):
            if attempt:
                self._stats.launch_retries += 1
                await asyncio.sleep(self._retry_backoff_base * attempt)
                # A failed attempt can leave profile locks / sessionstore
                # behind; never let it poison the retry with a stale lock.
                await asyncio.to_thread(_clean_launch_artifacts, slot.dir)
            try:
                lease = await self._launch_with_timeout(run_id, slot, display)
                break
            except Exception as exc:  # noqa: BLE001 - retry any launch failure
                last_error = exc
                logger.warning(
                    "browser launch attempt %s/%s for run %s failed: %s",
                    attempt + 1,
                    LAUNCH_ATTEMPTS,
                    run_id,
                    exc,
                )
        if lease is None:
            self._stats.launch_failures += 1
            self._stats.retire_reasons["launch_failed"] = (
                self._stats.retire_reasons.get("launch_failed", 0) + 1
            )
            await self._release_slot(run_id, slot, reason="launch_failed")
            raise BrowserLeaseError(
                f"browser launch failed after {LAUNCH_ATTEMPTS} attempts for run "
                f"{run_id}: {last_error}"
            ) from last_error
        self._leases[run_id] = lease
        self._stats.launches += 1
        self._stats.launch_durations.append(time.monotonic() - lease.launched_at)
        logger.info(
            "launched dedicated browser for run %s on %s (active=%s/%s)",
            run_id,
            slot.name,
            self.active_count,
            self._max_active,
        )
        return lease

    async def release(self, run_id: str, *, reason: str = "run_complete") -> None:
        """Close the instance (bounded), reset the slot, never block the pool."""
        lease = self._leases.pop(run_id, None)
        if lease is None:
            return
        self._stats.retire_reasons[reason] = self._stats.retire_reasons.get(reason, 0) + 1
        await self._close_bounded(lease)
        await self._release_slot(run_id, lease.slot, reason=reason)
        # Wake any waiter blocked in acquire() so it can grab the freed slot.
        self._release_event.set()

    async def close(self) -> None:
        """Close every lease and reset all slots."""
        for run_id in tuple(self._leases):
            await self.release(run_id, reason="pool_close")
        logger.info("browser pool closed: %s", self._stats.summary())

    # -- internals -----------------------------------------------------------

    async def _launch_with_timeout(
        self, run_id: str, slot: ProfileSlot, display: str | None
    ) -> BrowserLease:
        started = time.monotonic()
        server, backend, context = await asyncio.wait_for(
            self._launcher(run_id, slot.dir, display), timeout=self._launch_timeout
        )
        # Protocol-level health probe: process alive != protocol responsive.
        # Reaching this point means the browser launched; a dead pipe surfaces
        # as a hang or error on the context touch, caught by the timeout.
        await asyncio.wait_for(self._probe(context), timeout=self._probe_timeout)
        return BrowserLease(
            run_id=run_id,
            slot=slot,
            server=server,
            backend=backend,
            context=context,
            launched_at=started,
        )

    async def _probe(self, context: Any) -> None:
        # Cheap protocol touch: enumerating tabs round-trips through the
        # driver pipe. A dead Juggler/pipe raises here.
        with contextlib.suppress(Exception):
            tabs = context.tabs()
            if tabs is not None:
                return
        raise BrowserLeaseError("browser context did not respond to probe")

    async def _release_slot(self, run_id: str, slot: ProfileSlot, *, reason: str) -> None:
        try:
            await self._slots.release(slot)
        except Exception as exc:  # noqa: BLE001 - quarantine instead of blocking
            self._stats.quarantine_count += 1
            logger.error(
                "slot %s could not be reset after run %s (%s); quarantined: %s",
                slot.name,
                run_id,
                reason,
                exc,
            )

    async def _close_bounded(self, lease: BrowserLease) -> None:
        try:
            await asyncio.wait_for(
                lease.server.backend_pool.close_all(), timeout=self._close_timeout
            )
            self._stats.closes += 1
        except Exception as exc:  # noqa: BLE001 - a wedged close must not block the pool
            self._stats.close_failures += 1
            logger.warning("browser close for run %s timed out/failed: %s", lease.run_id, exc)
            # The slot reset below still runs; the orphaned browser is reaped
            # by the next provision() via _kill_processes_on_profile.

    @staticmethod
    async def _default_launcher(
        run_id: str, profile_dir: Path, display: str | None = None
    ) -> tuple[Any, Any, Any]:
        from playwright_python_mcp.mcp import create_connection  # local import

        server = await create_connection(
            build_browser_config(run_id, profile_dir=profile_dir, display=display)
        )
        backend = await server.backend_pool.backend_for(run_id)
        context = await backend._ensure_context(cwd=Path.cwd(), roots=None)
        return server, backend, context
