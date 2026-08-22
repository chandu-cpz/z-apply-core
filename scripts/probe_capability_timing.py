"""Probe: time inspect_page_capabilities against the REAL
Nielsen SmartRecruiters page in an isolated headless browser.

Measures total wall clock + per-phase breakdown to attribute the 58-96s
pre-router gaps attempt 7 showed on every turn.
"""

from __future__ import annotations

import asyncio
import time

from playwright.async_api import async_playwright

from z_apply_core.browser_form_inspection import (
    inspect_page_capabilities,
)
from z_apply_core.browser_targeting import empty_file_inputs

URL = "https://jobs.smartrecruiters.com/TheNielsenCompany/3743990014641116-ai-ml-data-scientist-i"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        t0 = time.monotonic()
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        print(f"page loaded in {time.monotonic() - t0:.1f}s (domcontentloaded)")
        await page.wait_for_timeout(3000)  # SPA settle like a real turn would see

        # Try to reach the actual application form (landing page has 0 controls).
        try:
            apply_btn = page.locator("button:has-text('Apply'), a:has-text('Apply')").first
            await apply_btn.click(timeout=8000)
            await page.wait_for_timeout(5000)
            print("clicked Apply; now on form iframe/page")
        except Exception as exc:
            print(f"Apply click skipped: {exc}")

        # Time the full capability inspection the way CapabilityContext runs it.
        for run in range(3):
            t0 = time.monotonic()
            caps = await inspect_page_capabilities(page)
            dt = time.monotonic() - t0
            print(
                f"inspect_page_capabilities #{run + 1}: {dt:.2f}s "
                f"controls={caps.editable_controls_visible} "
                f"unresolved={caps.unresolved_required_controls} "
                f"submit_en={caps.enabled_form_submit_visible}"
            )

        # Attribute inside: count controls and time one representative pass.
        from z_apply_core.browser_form_inspection import CONTROL_SELECTOR, _visible_enabled

        t0 = time.monotonic()
        controls = await _visible_enabled(page.locator(CONTROL_SELECTOR))
        print(
            f"_visible_enabled(CONTROL_SELECTOR): {time.monotonic() - t0:.2f}s, n={len(controls)}"
        )
        if controls:
            c = controls[0]
            t0 = time.monotonic()
            await c.get_attribute("type")
            print(f"single get_attribute roundtrip: {(time.monotonic() - t0) * 1000:.0f}ms")
        t0 = time.monotonic()
        await empty_file_inputs(page)
        print(f"empty_file_inputs: {time.monotonic() - t0:.2f}s")

        await browser.close()


asyncio.run(main())
