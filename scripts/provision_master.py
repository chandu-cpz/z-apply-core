"""Phase 0: provision a fresh sealed master profile with Simplify + uBlock.

Launches a headed Camoufox instance (on DISPLAY :0 when available) on a NEW
profile directory with the Simplify addon, holds it open so a human can log
into Simplify (email OTP) and accept the consent dialog, then — on the
sentinel file — closes the browser, verifies the addon state landed, cleans
non-essential site storage, and seals the profile read-only with a checksum
manifest.

The current master is never touched: this provisions ``<dir>.new`` and the
swap to ``browser-profile`` happens only after verification.

Usage:
    uv run python scripts/provision_master.py \
        --dir .z-apply/browser-profile.new \
        --wait-for /tmp/z-apply-provision-done \
        --timeout 1800
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
from pathlib import Path

from z_apply_core.browser_config import build_browser_config
from z_apply_core.config import CORE_ROOT

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("provision-master")

# Directories/files in a Firefox profile that are disposable per-machine state
# and must NOT become part of the sealed master.
_DISPOSABLE_TOP_LEVEL = {
    "cache2",
    "startupCache",
    "shader-cache",
    "datareporting",
    "crashes",
    "sessionstore-backups",
    "bounce-tracking-protection.sqlite",
    "places.sqlite",
    "places.sqlite-wal",
    "places.sqlite-shm",
    "favicons.sqlite",
    "favicons.sqlite-shm",
    "formhistory.sqlite",
    "sessionstore.jsonlz4",
    "sessionCheckpoints.json",
    "addonStartup.json.lz4",
    ".parentlock",
    "lock",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seal(profile_dir: Path) -> dict[str, object]:
    """Remove disposable state, chmod read-only, write the checksum manifest."""
    for name in _DISPOSABLE_TOP_LEVEL:
        path = profile_dir / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            # Broken symlinks (Firefox 'lock') report exists()=False.
            path.unlink(missing_ok=True)
    # Drop sqlite journals left by the provisioning session.
    for journal in profile_dir.rglob("*.sqlite-wal"):
        journal.unlink(missing_ok=True)
    for journal in profile_dir.rglob("*.sqlite-shm"):
        journal.unlink(missing_ok=True)

    manifest: dict[str, object] = {
        "sealed_at": __import__("datetime").datetime.now().isoformat(),
        "files": {},
    }
    files: dict[str, str] = {}
    for path in sorted(profile_dir.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(profile_dir))] = _sha256(path)
    manifest["files"] = files

    # Read-only everywhere: the master is never launched, only rsync-copied.
    for path in profile_dir.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
    profile_dir.chmod(profile_dir.stat().st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)

    manifest_path = profile_dir.parent / f"{profile_dir.name}.seal.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _verify_addons(profile_dir: Path) -> list[str]:
    """Return a list of problems; empty = the addon state landed."""
    problems: list[str] = []
    extensions = profile_dir / "extensions.json"
    if not extensions.exists():
        problems.append("extensions.json missing")
    storage_default = profile_dir / "storage" / "default"
    if not storage_default.is_dir():
        problems.append("storage/default missing")
        return problems
    addon_dirs = [p.name for p in storage_default.iterdir() if p.name.startswith("moz-extension")]
    if not addon_dirs:
        problems.append("no moz-extension addon storage — Simplify did not run")
    else:
        logger.info("addon storage present: %s", ", ".join(addon_dirs))
    return problems


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(CORE_ROOT / ".z-apply" / "browser-profile.new"))
    parser.add_argument("--wait-for", default="/tmp/z-apply-provision-done")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    profile_dir = Path(args.dir).resolve()
    sentinel = Path(args.wait_for)
    sentinel.unlink(missing_ok=True)
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
    profile_dir.mkdir(parents=True)

    config = build_browser_config("provision-master", profile_dir=profile_dir)
    os.environ.setdefault("DISPLAY", ":0")

    from playwright_python_mcp.mcp import create_connection

    logger.info("launching provisioning browser on %s (DISPLAY=%s)", profile_dir, os.environ["DISPLAY"])
    server = await create_connection(config)
    backend = await server.backend_pool.backend_for("__provision__")
    await backend._ensure_context(cwd=Path.cwd(), roots=None)
    logger.info(
        "browser is UP on your screen. Log into simplify.jobs, accept the consent "
        "on a job site if it appears, then: touch %s",
        sentinel,
    )

    waited = 0.0
    while not sentinel.exists() and waited < args.timeout:
        await asyncio.sleep(2)
        waited += 2
    if not sentinel.exists():
        logger.error("timed out after %ss waiting for %s", args.timeout, sentinel)
        await server.backend_pool.close_all()
        shutil.rmtree(profile_dir, ignore_errors=True)
        return 1

    await server.backend_pool.close_all()
    logger.info("browser closed")

    problems = _verify_addons(profile_dir)
    if problems:
        logger.error("verification FAILED: %s", ", ".join(problems))
        return 1

    # Clean site storage down to the extension data (consent + profile live in
    # moz-extension dirs; simplify.jobs storage is the login session).
    keep = {"chrome", "ls-archive.sqlite"}
    for entry in (profile_dir / "storage" / "default").iterdir():
        if entry.name.startswith("moz-extension"):
            keep.add(entry.name)
    for entry in (profile_dir / "storage" / "default").iterdir():
        if entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(
                missing_ok=True
            )

    manifest = _seal(profile_dir)
    logger.info("SEALED %s — %d file(s), %d addon storage dir(s)",
                profile_dir, len(manifest["files"]), len(
                    [p for p in profile_dir.rglob("storage/default/*") if p.name.startswith("moz-extension")]
                ))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
