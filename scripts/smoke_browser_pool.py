"""Live smoke test for the browser pool: real master, real Camoufox launch.

Validates the full Phase 1-3 path against the actual sealed master profile:
provision slots -> acquire (launches a REAL dedicated Camoufox instance on the
slot) -> verify the browser is usable -> release (bounded close + rsync reset).

Run:  uv run python scripts/smoke_browser_pool.py [--instances N] [--keep]
Exit 0 = green; nonzero = red. Optional --keep leaves the last browser open
for manual inspection (then press Ctrl-C to tear down).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from z_apply_core.browser_pool import BrowserPool
from z_apply_core.config import CORE_ROOT

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("smoke")

MASTER = CORE_ROOT / ".z-apply" / "browser-profile"
SLOTS_ROOT = CORE_ROOT / ".z-apply" / "profiles"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", type=int, default=1)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if not MASTER.is_dir():
        logger.error("master profile missing: %s", MASTER)
        return 1

    pool = BrowserPool(master=MASTER, slots_root=SLOTS_ROOT, slot_count=args.instances)
    await pool.provision()
    logger.info("provisioned %d slot(s) from %s", args.instances, MASTER)

    leases = []
    try:
        for i in range(args.instances):
            started = time.monotonic()
            lease = await pool.acquire(f"smoke-{i}")
            elapsed = time.monotonic() - started
            tabs = tuple(lease.context.tabs())
            pages = [t.page for t in tabs if not t.page.is_closed()]
            logger.info(
                "smoke-%d: launched on %s in %.1fs, %d live page(s)",
                i,
                lease.profile_dir,
                elapsed,
                len(pages),
            )
            if not pages:
                logger.error("smoke-%d: no usable page in launched instance", i)
                return 1
            leases.append(lease)
        logger.info("ALL %d INSTANCES UP — pool is healthy", args.instances)
        if args.keep:
            logger.info("keeping browsers open; Ctrl-C to tear down")
            await asyncio.Event().wait()
    finally:
        if not args.keep:
            for i in range(args.instances - 1, -1, -1):
                await pool.release(f"smoke-{i}")
            logger.info("released all instances; slots reset")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
