"""Phase 0: provision a sealed master profile with Simplify + uBlock.

Launches a headed Camoufox instance (on DISPLAY :0 when available) on a NEW
profile directory with the Simplify addon installed as a real app-profile
sideload, holds it open so a human can log into Simplify (email OTP) and
accept the consent dialog if it appears, then — on the sentinel file — closes
the browser, verifies the addon state landed, cleans non-essential site
storage, and seals the profile read-only with a checksum manifest.

The current master is never touched: this provisions ``<dir>.new`` and the
swap to ``browser-profile`` happens only after verification. Pass ``--from``
to seed the new profile from an existing one (keeps the login session and any
already-baked consent instead of re-entering them).

Usage:
    uv run python scripts/provision_master.py \
        --dir .z-apply/browser-profile.new \
        --wait-for /tmp/z-apply-provision-done \
        --timeout 1800
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path

from z_apply_core.browser_config import build_browser_config
from z_apply_core.config import CORE_ROOT, load_settings
from z_apply_core.profile_pool import SIMPLIFY_ADDON_ID

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


def _make_writable(profile_dir: Path) -> None:
    """The master is sealed read-only; a provisioning copy must be writable."""
    for path in profile_dir.rglob("*"):
        with contextlib.suppress(OSError):
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
    with contextlib.suppress(OSError):
        profile_dir.chmod(profile_dir.stat().st_mode | stat.S_IWUSR)


def _bake_simplify_xpi(profile_dir: Path, addon_dir: Path) -> Path:
    """Install the Simplify addon as a real app-profile sideload.

    The addon ships to every slot as ``<profile>/extensions/<id>.xpi`` so its
    moz-extension UUID is stable across runs (Firefox persists it in the
    ``extensions.webextensions.uuids`` pref) and its ``storage.local`` consent
    survives; a temporary install churns the UUID on every launch and orphans
    the consent.
    """
    addon_dir = addon_dir.expanduser().resolve()
    if not addon_dir.is_dir():
        raise ValueError(f"Configured Simplify addon directory does not exist: {addon_dir}")
    extensions_dir = profile_dir / "extensions"
    extensions_dir.mkdir(exist_ok=True)
    xpi = extensions_dir / f"{SIMPLIFY_ADDON_ID}.xpi"
    with zipfile.ZipFile(xpi, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(addon_dir.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(str(path.relative_to(addon_dir)), date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    logger.info("baked Simplify sideload: %s", xpi)
    return xpi


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
    else:
        try:
            manifest = json.loads(extensions.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
        sideloaded = any(
            a.get("id") == SIMPLIFY_ADDON_ID
            and a.get("active") is True
            and a.get("location") == "app-profile"
            for a in manifest.get("addons", [])
        )
        if not sideloaded:
            problems.append("Simplify addon is not an active app-profile sideload")
        elif not (profile_dir / "extensions" / f"{SIMPLIFY_ADDON_ID}.xpi").is_file():
            problems.append("Simplify sideload xpi missing from extensions/")
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
    parser.add_argument("--from", dest="seed_from", default=None,
                        help="existing profile to seed from (keeps login session + baked consent)")
    parser.add_argument("--wait-for", default="/tmp/z-apply-provision-done")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    profile_dir = Path(args.dir).resolve()
    sentinel = Path(args.wait_for)
    sentinel.unlink(missing_ok=True)
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
    if args.seed_from:
        seed = Path(args.seed_from).resolve()
        if not seed.is_dir():
            logger.error("--from profile does not exist: %s", seed)
            return 1
        shutil.copytree(seed, profile_dir)
        _make_writable(profile_dir)
        logger.info("seeded new profile from %s", seed)
    else:
        profile_dir.mkdir(parents=True)

    _bake_simplify_xpi(profile_dir, load_settings().simplify_addon_path)

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
