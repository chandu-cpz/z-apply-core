"""Probe v3: inspect_page_capabilities scaling vs control
count and main-thread jank, over a REAL playwright browser.

Question: can capability inspection plausibly reach 58-96s/turn at production
form sizes (30-80 controls) under SPA jank?
"""

from __future__ import annotations

import asyncio
import time

from playwright.async_api import async_playwright

from z_apply_core.browser_form_inspection import inspect_page_capabilities

PAGE_TMPL = """
<html><body><form id='app'>
{controls}
</form>
<script>
// Simulate SPA main-thread churn: 150ms of blocking work every 400ms.
setInterval(() => {{
    const start = performance.now();
    while (performance.now() - start < 150) {{ Math.random(); }}
}}, 400);
</script>
</body></html>
"""


def controls(n: int) -> str:
    fields = []
    for i in range(n):
        fields.append(f"<input name='f{i}' placeholder='Field {i}'>")
        if i % 5 == 0:
            fields.append(f"<select name='s{i}'><option>1</option></select>")
        if i % 10 == 0:
            fields.append(f"<button type='submit' disabled>Save {i}</button>")
    return "\n".join(fields)


async def timed(page, label: str) -> None:
    t0 = time.monotonic()
    caps = await inspect_page_capabilities(page)
    print(
        f"{label:24s} {time.monotonic() - t0:7.2f}s  unresolved={caps.unresolved_required_controls}"
    )


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("about:blank")

        for n in (10, 40, 80):
            await page.set_content(PAGE_TMPL.format(controls=controls(n)))
            await page.wait_for_timeout(600)
            await timed(page, f"{n} controls + jank")

        # Extreme: 200 controls, heavy jank.
        await page.set_content(PAGE_TMPL.format(controls=controls(200)))
        await page.wait_for_timeout(600)
        await timed(page, "200 controls + jank")

        await browser.close()


asyncio.run(main())
