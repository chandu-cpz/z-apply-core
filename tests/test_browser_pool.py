from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from z_apply_core import browser_pool
from z_apply_core.browser_pool import (
    BrowserLeaseError,
    BrowserPool,
    PoolCapacityError,
)
from z_apply_core.profile_pool import ProfileSlotError


def _make_master(root: Path) -> Path:
    """Build a synthetic sealed master profile (addons + persisted state)."""
    master = root / "master"
    (master / "storage" / "default" / "moz-extension+++addon-1").mkdir(parents=True)
    (master / "storage" / "default" / "https+++example.test").mkdir(parents=True)
    (master / "extensions.json").write_text("{}")
    (master / "extension-preferences.json").write_text("{}")
    (master / "extension-settings.json").write_text("{}")
    (master / "prefs.js").write_text("user_pref('x', true);")
    (master / "cookies.sqlite").write_bytes(b"cookies")
    (master / "permissions.sqlite").write_bytes(b"permissions")
    (master / "logins.json").write_text("{}")
    (master / "key4.db").write_bytes(b"key")
    (master / "cert9.db").write_bytes(b"cert")
    (master / "cache2").mkdir()
    (master / "cache2" / "junk").write_bytes(b"junk")
    (master / "sessionstore.jsonlz4").write_bytes(b"session")
    (master / ".parentlock").write_bytes(b"lock")
    return master


class FakeBackendPool:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def close_all(self) -> None:
        self.close_calls += 1
        self.closed = True


class FakeContext:
    def tabs(self) -> list[Any]:  # noqa: ANN401
        return []


class FakeServer:
    def __init__(self) -> None:
        self.backend_pool = FakeBackendPool()


class RecordingLauncher:
    """Fake launcher that records profile dirs and can be made to fail/hang."""

    def __init__(self) -> None:
        self.launched: list[Path] = []
        self.fail_times: dict[str, int] = {}  # run_id -> failures before success
        self._failures: dict[str, int] = {}
        self.hang = False

    async def __call__(self, run_id: str, profile_dir: Path) -> tuple[FakeServer, object, FakeContext]:
        if self.hang:
            await asyncio.sleep(60)
        if self._failures.get(run_id, 0) < self.fail_times.get(run_id, 0):
            self._failures[run_id] = self._failures.get(run_id, 0) + 1
            raise RuntimeError("transient launch failure")
        self.launched.append(profile_dir)
        return FakeServer(), object(), FakeContext()


class BrowserPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self.master = _make_master(self._root)
        self.launcher = RecordingLauncher()
        self.pool = BrowserPool(
            master=self.master,
            slots_root=self._root / "profiles",
            slot_count=2,
            max_active=2,
            launcher=self.launcher,
            launch_timeout=5,
            probe_timeout=2,
            close_timeout=2,
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_acquire_launches_on_the_leased_slot_dir(self) -> None:
        await self.pool.provision()
        lease = await self.pool.acquire("run-1")
        self.assertEqual(self.launcher.launched, [lease.profile_dir])
        self.assertTrue(str(lease.profile_dir).endswith("slot-1"))
        self.assertEqual(self.pool.active_count, 1)
        self.assertIs(self.pool.lease("run-1"), lease)

    async def test_release_closes_server_resets_slot_and_clears_marker(self) -> None:
        await self.pool.provision()
        lease = await self.pool.acquire("run-1")
        # Dirty the slot as a run would.
        (lease.profile_dir / "stale.txt").write_text("dirty")
        (lease.profile_dir / "storage" / "default" / "https+++ran.test").mkdir(parents=True)
        await self.pool.release("run-1")
        self.assertTrue(lease.server.backend_pool.closed)
        self.assertEqual(self.pool.active_count, 0)
        self.assertFalse((lease.profile_dir / "stale.txt").exists())
        self.assertFalse((lease.profile_dir / "storage" / "default" / "https+++ran.test").exists())
        self.assertFalse(lease.slot.lease_marker.exists())
        self.assertEqual(lease.slot.state, "pristine")

    async def test_capacity_rejects_over_max_active(self) -> None:
        await self.pool.provision()
        await self.pool.acquire("run-1")
        await self.pool.acquire("run-2")
        with self.assertRaises(PoolCapacityError):
            await self.pool.acquire("run-3")
        self.assertEqual(self.pool.stats.capacity_rejections, 1)

    async def test_duplicate_run_id_is_rejected(self) -> None:
        await self.pool.provision()
        await self.pool.acquire("run-1")
        with self.assertRaises(BrowserLeaseError):
            await self.pool.acquire("run-1")

    async def test_transient_launch_failure_retries_and_succeeds(self) -> None:
        await self.pool.provision()
        self.launcher.fail_times["run-1"] = 2  # fail twice, then succeed
        lease = await self.pool.acquire("run-1")
        self.assertIsNotNone(lease)
        self.assertGreaterEqual(self.pool.stats.launch_retries, 2)

    async def test_persistent_launch_failure_releases_the_slot(self) -> None:
        await self.pool.provision()
        self.launcher.fail_times["run-1"] = 99
        with self.assertRaises(BrowserLeaseError):
            await self.pool.acquire("run-1")
        # The failed slot was released back to the pool (clean for reuse).
        free = [s for s in self.pool._slots.slots if s.state == "pristine"]
        self.assertEqual(len(free), 2)
        self.assertEqual(self.pool.stats.launch_failures, 1)

    async def test_launch_hang_is_bounded_by_timeout(self) -> None:
        await self.pool.provision()
        self.launcher.hang = True
        pool = BrowserPool(
            master=self.master,
            slots_root=self._root / "profiles",
            slot_count=2,
            max_active=2,
            launcher=self.launcher,
            launch_timeout=0.3,
            probe_timeout=0.2,
            close_timeout=2,
            retry_backoff_base=0.01,
        )
        await pool.provision()
        with self.assertRaises(BrowserLeaseError):
            await pool.acquire("run-1")
        self.assertEqual(pool.active_count, 0)

    async def test_no_slots_free_raises_profile_slot_error(self) -> None:
        # slot_count=1 pool: one lease + one failed lease exhausts slots.
        pool = BrowserPool(
            master=self.master,
            slots_root=self._root / "profiles",
            slot_count=1,
            max_active=2,
            launcher=self.launcher,
            launch_timeout=5,
            probe_timeout=2,
            close_timeout=2,
            retry_backoff_base=0.01,
        )
        await pool.provision()
        await pool.acquire("run-1")
        # Slot exhaustion happens at selection, before any launch attempt.
        with self.assertRaises(ProfileSlotError):
            await pool.acquire("run-2")
        await pool.release("run-1")

    async def test_stats_summary_shape(self) -> None:
        await self.pool.provision()
        await self.pool.acquire("run-1")
        await self.pool.release("run-1")
        summary = self.pool.stats.summary()
        self.assertEqual(summary["launches"], 1)
        self.assertEqual(summary["closes"], 1)
        self.assertIn("run_complete", summary["retire_reasons"])
        self.assertGreaterEqual(summary["launch_p95_s"], 0)

    async def test_kill_processes_on_profile_is_safe_on_missing_dir(self) -> None:
        # No processes reference a synthetic slot path; must be a no-op.
        await asyncio.to_thread(
            browser_pool._kill_processes_on_profile, self._root / "nonexistent-slot"
        )


if __name__ == "__main__":
    unittest.main()
