"""Per-run profile slots for isolated Camoufox instances.

Phase 1 of the multi-browser pool: a fixed set of pristine Firefox profile
copies mirrored from a read-only *master* profile. Each slot is a disjoint
profile directory that exactly one run's browser instance may launch on at a
time (Firefox locks a profile directory to a single process).

The master is never written by runs: slots are disposable mirrors, reset with
``rsync --delete`` after every run. No state is ever merged back (no
sync-back), so per-run profiles never accumulate per-site cookies, caches or
captcha evidence — every run starts from the same sealed image.

Slots are tracked with an out-of-band lease marker (``<slot>.lease`` next to
the profile directory, never inside it) so a crashed process can be reconciled
by the next startup: an orphaned lease means the slot is dirty and must be
reset before it is leased again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from z_apply_core.config import CORE_ROOT

logger = logging.getLogger(__name__)

PROFILES_ROOT = CORE_ROOT / ".z-apply" / "profiles"
DEFAULT_MASTER_PROFILE = CORE_ROOT / ".z-apply" / "browser-profile"
DEFAULT_SLOT_COUNT = 3

SlotState = Literal["pristine", "leased", "resetting", "quarantined"]

# Files/dirs that carry installed addons and their persisted state. Firefox
# profile copies need exactly these; everything else is disposable cache or
# per-machine state that must NOT leak between runs.
RSYNC_INCLUDES = (
    "storage/",
    "storage/**",
    "extensions.json",
    "extension-preferences.json",
    "extension-settings.json",
    "prefs.js",
    "cookies.sqlite",
    "permissions.sqlite",
    "logins.json",
    "logins.db",
    "key4.db",
    "cert9.db",
    # Extension sync storage + the per-site IndexedDB registry: small but
    # carry extension state (e.g. storage.sync) the storage/ tree misses.
    "storage-sync-v2.sqlite",
    "storage.sqlite",
)
# Catch-all: drop caches, sessionstore, sqlite journals, lock files and every
# other file not explicitly included above.
RSYNC_EXCLUDES = ("*",)

# Session-restore artifacts that make Firefox hang when a profile is reused
# (playwright #12632: launchPersistentContext times out on profile relaunch).
SESSIONSTORE_ARTIFACTS = (
    "sessionstore-backups",
    "sessionCheckpoints.json",
    "sessionstore.jsonlz4",
)

# Firefox profile lock files. A leftover lock after an unclean shutdown blocks
# a relaunch on the same directory.
LOCK_FILES = (".parentlock", "lock")

# Files that must exist in a slot before a browser may launch on it.
REQUIRED_MANIFEST = ("extensions.json", "prefs.js", "cookies.sqlite", "storage")


class ProfileSlotError(RuntimeError):
    """A slot could not be provisioned, reset, or verified."""


def _rsync_mirror(master: Path, slot: Path) -> None:
    """Mirror the filtered master profile into ``slot`` (idempotent reset).

    ``--delete --delete-excluded`` guarantees the slot is an exact mirror of
    the *filtered* master: stale files left by a previous run (cookies,
    site storage, sessionstore, locks) are removed, and every included file is
    restored byte-for-byte from the sealed master.
    """
    if not master.is_dir():
        raise ProfileSlotError(f"master profile does not exist: {master}")
    slot.mkdir(parents=True, exist_ok=True)
    cmd = [
        "rsync",
        "-a",
        "--delete",
        "--delete-excluded",
        # The master is sealed read-only (444/555); the slot is a WORKING
        # copy that Firefox must be able to write (prefs, cookies, locks).
        # Restore writable perms so a slot is never read-only.
        "--chmod=u+rw,go-w",
        *[f"--include={pattern}" for pattern in RSYNC_INCLUDES],
        *[f"--exclude={pattern}" for pattern in RSYNC_EXCLUDES],
        f"{master}/",
        f"{slot}/",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProfileSlotError(
            f"rsync mirror failed ({result.returncode}): {result.stderr.strip()}"
        )


def verify_manifest(slot: Path, *, require_addon_storage: bool = True) -> None:
    """Fail-fast check that a slot is a complete, launchable profile copy.

    Raises ``ProfileSlotError`` if any required file is missing, or — when
    ``require_addon_storage`` is set — if no addon (moz-extension) storage
    survived the copy. Never launch a partial profile.
    """
    missing = [name for name in REQUIRED_MANIFEST if not (slot / name).exists()]
    if missing:
        raise ProfileSlotError(f"slot is missing required files: {missing}")
    if require_addon_storage:
        storage_default = slot / "storage" / "default"
        if not storage_default.is_dir() or not any(
            entry.is_dir() and entry.name.startswith("moz-extension")
            for entry in storage_default.iterdir()
        ):
            raise ProfileSlotError("slot has no moz-extension addon storage")


def _clean_launch_artifacts(slot: Path) -> None:
    """Remove session-restore state and stale profile locks from a slot."""
    for name in SESSIONSTORE_ARTIFACTS:
        path = slot / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    for name in LOCK_FILES:
        (slot / name).unlink(missing_ok=True)


@dataclass
class ProfileSlot:
    """One disjoint profile directory. The unit of browser isolation."""

    index: int
    root: Path
    master: Path
    state: SlotState = "pristine"
    run_id: str | None = None

    @property
    def dir(self) -> Path:
        return self.root

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def lease_marker(self) -> Path:
        return self.root.parent / f"{self.root.name}.lease"

    def mark_leased(self, run_id: str) -> None:
        self.state = "leased"
        self.run_id = run_id
        self.lease_marker.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")

    def clear_lease(self) -> None:
        self.state = "pristine"
        self.run_id = None
        self.lease_marker.unlink(missing_ok=True)

    async def reset(self) -> None:
        """Re-mirror from master and clean launch artifacts (no sync-back)."""
        self.state = "resetting"
        try:
            await asyncio.to_thread(_rsync_mirror, self.master, self.root)
            await asyncio.to_thread(_clean_launch_artifacts, self.root)
            await asyncio.to_thread(verify_manifest, self.root)
            self.state = "pristine"
        except Exception:
            self.state = "quarantined"
            raise

    async def quarantine(self, reason: str) -> None:
        logger.error("quarantining slot %s: %s", self.name, reason)
        self.state = "quarantined"
        self.clear_lease()


class ProfileSlotPool:
    """Fixed pool of pristine profile slots leased to runs one at a time."""

    def __init__(
        self,
        *,
        master: Path = DEFAULT_MASTER_PROFILE,
        slots_root: Path = PROFILES_ROOT,
        slot_count: int = DEFAULT_SLOT_COUNT,
    ) -> None:
        self.master = master
        self.slots_root = slots_root
        self.slot_count = slot_count
        self.slots: list[ProfileSlot] = []
        self._lock = asyncio.Lock()  # serialize acquire/release (camoufox #553)

    async def provision(self) -> None:
        """Create slot directories, reconcile orphaned leases, mirror master."""
        async with self._lock:
            self.slots_root.mkdir(parents=True, exist_ok=True)
            self.slots = [
                ProfileSlot(index=i, root=self.slots_root / f"slot-{i + 1}", master=self.master)
                for i in range(self.slot_count)
            ]
            for slot in self.slots:
                if slot.lease_marker.exists():
                    # A previous process died mid-lease: the slot is dirty.
                    logger.warning(
                        "reconciling orphaned lease on %s: %s", slot.name, slot.lease_marker
                    )
                await slot.reset()
                slot.lease_marker.unlink(missing_ok=True)

    async def acquire(self, run_id: str) -> ProfileSlot:
        """Return a pristine slot leased to ``run_id``.

        Raises ``ProfileSlotError`` if no slot is available (queue/admission
        control lives in the pool layer that consumes this).
        """
        async with self._lock:
            for slot in self.slots:
                if slot.state == "pristine":
                    slot.mark_leased(run_id)
                    return slot
            raise ProfileSlotError(
                f"no free profile slot for run {run_id} "
                f"(slots: {[(s.name, s.state) for s in self.slots]})"
            )

    async def release(self, slot: ProfileSlot) -> None:
        """Reset the slot from master and return it to the pool. No sync-back."""
        async with self._lock:
            await slot.reset()
            slot.clear_lease()

    def slot_for(self, run_id: str) -> ProfileSlot | None:
        return next((s for s in self.slots if s.run_id == run_id), None)
