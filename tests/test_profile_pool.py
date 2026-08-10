from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from z_apply_core import profile_pool
from z_apply_core.profile_pool import LOCK_FILES, SESSIONSTORE_ARTIFACTS


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
    # Disposable junk that must never reach a slot.
    (master / "cache2").mkdir()
    (master / "cache2" / "junk").write_bytes(b"junk")
    (master / "startupCache").mkdir()
    (master / "datareporting").mkdir()
    (master / "places.sqlite").write_bytes(b"places")
    (master / "favicons.sqlite").write_bytes(b"favicons")
    (master / "sessionstore.jsonlz4").write_bytes(b"session")
    (master / "cookies.sqlite-wal").write_bytes(b"wal")
    (master / ".parentlock").write_bytes(b"lock")
    return master


class ProfileSlotPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self.master = _make_master(self._root)
        self.pool = profile_pool.ProfileSlotPool(
            master=self.master,
            slots_root=self._root / "profiles",
            slot_count=2,
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_provision_mirrors_only_included_files(self) -> None:
        await self.pool.provision()
        slot = self.pool.slots[0]
        self.assertEqual(slot.state, "pristine")
        self.assertTrue((slot.dir / "extensions.json").exists())
        self.assertTrue((slot.dir / "storage" / "default" / "moz-extension+++addon-1").is_dir())
        # Disposable profile junk must not leak into slots.
        for name in ("cache2", "startupCache", "datareporting", "places.sqlite",
                     "favicons.sqlite", "sessionstore.jsonlz4", "cookies.sqlite-wal"):
            self.assertFalse((slot.dir / name).exists(), name)

    async def test_reset_removes_stale_run_state(self) -> None:
        await self.pool.provision()
        slot = self.pool.slots[0]
        # Simulate a run that left site state + caches behind.
        (slot.dir / "storage" / "default" / "https+++ran-site.test").mkdir(parents=True)
        (slot.dir / "cache2").mkdir()
        (slot.dir / "stale.txt").write_text("stale")
        (slot.dir / "cookies.sqlite").write_bytes(b"corrupted-by-run")
        await slot.reset()
        self.assertFalse((slot.dir / "storage" / "default" / "https+++ran-site.test").exists())
        self.assertFalse((slot.dir / "cache2").exists())
        self.assertFalse((slot.dir / "stale.txt").exists())
        self.assertEqual((slot.dir / "cookies.sqlite").read_bytes(), b"cookies")

    async def test_prepare_cleans_sessionstore_and_locks(self) -> None:
        await self.pool.provision()
        slot = self.pool.slots[0]
        (slot.dir / "sessionstore-backups").mkdir()
        (slot.dir / "sessionCheckpoints.json").write_text("{}")
        (slot.dir / "sessionstore.jsonlz4").write_bytes(b"s")
        (slot.dir / ".parentlock").write_bytes(b"lock")
        (slot.dir / "lock").write_bytes(b"lock")
        await asyncio.to_thread(profile_pool._clean_launch_artifacts, slot.dir)
        for name in SESSIONSTORE_ARTIFACTS + LOCK_FILES:
            self.assertFalse((slot.dir / name).exists(), name)

    async def test_verify_manifest_rejects_partial_slot(self) -> None:
        await self.pool.provision()
        slot = self.pool.slots[0]
        (slot.dir / "extensions.json").unlink()
        with self.assertRaises(profile_pool.ProfileSlotError):
            await asyncio.to_thread(profile_pool.verify_manifest, slot.dir)
        (slot.dir / "extensions.json").write_text("{}")
        (slot.dir / "storage" / "default" / "moz-extension+++addon-1").rmdir()
        with self.assertRaises(profile_pool.ProfileSlotError):
            await asyncio.to_thread(profile_pool.verify_manifest, slot.dir)

    async def test_lease_acquire_release_cycle(self) -> None:
        await self.pool.provision()
        slot = await self.pool.acquire("run-1")
        self.assertEqual(slot.state, "leased")
        self.assertTrue(slot.lease_marker.exists())
        self.assertIs(self.pool.slot_for("run-1"), slot)
        slot2 = await self.pool.acquire("run-2")
        self.assertIsNot(slot, slot2)
        with self.assertRaises(profile_pool.ProfileSlotError):
            await self.pool.acquire("run-3")  # all 2 slots leased
        await self.pool.release(slot)
        self.assertEqual(slot.state, "pristine")
        self.assertFalse(slot.lease_marker.exists())
        await self.pool.release(slot2)

    async def test_release_resets_run_state_and_lease(self) -> None:
        await self.pool.provision()
        slot = await self.pool.acquire("run-1")
        (slot.dir / "storage" / "default" / "https+++ran.test").mkdir(parents=True)
        await self.pool.release(slot)
        self.assertFalse((slot.dir / "storage" / "default" / "https+++ran.test").exists())
        self.assertEqual(slot.state, "pristine")
        self.assertIsNone(slot.run_id)

    async def test_orphaned_lease_is_reconciled_on_provision(self) -> None:
        await self.pool.provision()
        slot = self.pool.slots[0]
        slot.mark_leased("dead-run")
        (slot.dir / "stale.txt").write_text("dirty")
        # Simulate a fresh process provisioning the pool: the orphaned lease
        # marker triggers a reset of that slot.
        fresh = profile_pool.ProfileSlotPool(
            master=self.master,
            slots_root=self._root / "profiles",
            slot_count=2,
        )
        await fresh.provision()
        self.assertEqual(fresh.slots[0].state, "pristine")
        self.assertFalse((fresh.slots[0].dir / "stale.txt").exists())
        self.assertFalse(fresh.slots[0].lease_marker.exists())


if __name__ == "__main__":
    unittest.main()
