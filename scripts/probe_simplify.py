"""Probe the Simplify panel on the greenhouse job page (form NOT open)."""

import asyncio
import contextlib
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "src")
from z_apply_core.browser_config import build_browser_config
from z_apply_core.config import CORE_ROOT
from z_apply_core.virtual_display import VirtualDisplaySession

PROFILE = CORE_ROOT / ".z-apply" / "probe-simplify"


async def main() -> None:
    if PROFILE.exists():
        shutil.rmtree(PROFILE, ignore_errors=True)
    # Seed from the sealed master (login + consent) so Simplify is authenticated.
    import shutil as _sh

    master = CORE_ROOT / ".z-apply" / "browser-profile"
    _sh.copytree(master, PROFILE)
    for path in PROFILE.rglob("*"):
        with contextlib.suppress(OSError):
            path.chmod(path.stat().st_mode | 0o600)

    display = VirtualDisplaySession(enabled=True)
    display.start()
    try:
        from playwright_python_mcp.mcp import create_connection

        server = await create_connection(
            build_browser_config("probe-simplify", profile_dir=PROFILE, display=display.display)
        )
        backend = await server.backend_pool.backend_for("__ps__")
        ctx = await backend._ensure_context(cwd=Path.cwd(), roots=None)
        tabs = tuple(ctx.tabs())
        if not tabs:
            await ctx.new_tab()
        await backend.call_tool(
            "browser_navigate",
            {"url": "https://job-boards.greenhouse.io/insurityindia/jobs/4354932009"},
        )
        await asyncio.sleep(12)
        # What does the Simplify shadow root show on the JOB page (form NOT open)?
        snap = str(
            await backend.call_tool("browser_snapshot", {"target": ".simplify-jobs-shadow-root"})
        )
        m = re.search(r"### Snapshot\s*```yaml(.*?)```", snap, re.S)
        print("=== SIMPLIFY SHADOW ROOT ON JOB PAGE ===")
        print((m.group(1) if m else snap)[:1500])
        await server.backend_pool.close_all()
    finally:
        display.stop()
        shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
