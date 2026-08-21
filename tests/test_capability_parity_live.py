"""Live parity harness for the capability batch (OPT-DEC-010 H2 acceptance).

Runs BOTH inspection paths against real DOM under Xvfb Firefox and asserts
field-for-field equality of every semantic BrowserCapabilities field.

Gated behind Z_APPLY_LIVE_PARITY=1 because it launches a real browser; the
landing pipeline runs it explicitly.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any

from playwright.async_api import async_playwright

from z_apply_core.browser_capability_batch import (
    inspect_page_capabilities_batched,
)
from z_apply_core.browser_form_inspection import inspect_page_capabilities
from z_apply_core.browser_observation import BrowserCapabilities

FIXTURE = """
<html><body style="margin:0">
  <form id="main">
    <input id="fn" name="firstname" required value="Chandrakanth">
    <input name="lastname" required>
    <input type="radio" name="tier" required value="a">
    <input type="radio" name="tier" required value="b" checked>
    <input type="checkbox" name="news" checked>
    <select name="country" required><option value="">pick</option><option>India</option></select>
    <textarea aria-invalid="true" name="msg">hello</textarea>
    <div contenteditable="true" name="bio"></div>
    <input type="email" value="not-an-email">
    <button type="submit" id="go">Submit</button>
    <button type="submit" disabled>Disabled submit</button>
  </form>

  <form role="search" id="sitesearch">
    <input type="search" name="q">
    <button type="submit">Search</button>
  </form>

  <form id="proxyform">
    <input name="pf" required>
    <button type="button" id="proxybtn">Continue</button>
  </form>

  <input type="password" autocomplete="current-password">

  <input type="file" id="reqfile" required>
  <input type="file" id="optfile">
  <input type="file" id="filledfile" required>

  <canvas id="bigcanvas" width="1200" height="900"></canvas>
  <script>
    const dt = new DataTransfer();
    dt.items.add(new File(['x'], 'resume.pdf'));
    document.getElementById('filledfile').files = dt.files;
  </script>
</body></html>
"""


async def _both_paths(page: Any) -> tuple[BrowserCapabilities, BrowserCapabilities]:  # noqa: ANN401
    legacy = await inspect_page_capabilities(page)
    batched = await inspect_page_capabilities_batched(page, timeout_s=5.0)
    return legacy, batched


def _semantic(caps: BrowserCapabilities) -> dict[str, Any]:
    return {
        field: getattr(caps, field)
        for field in caps.__dataclass_fields__
        if field not in {"inspection_ms", "controls_scanned"}
    }


@unittest.skipUnless(os.environ.get("Z_APPLY_LIVE_PARITY") == "1", "needs a real browser")
class LiveParityTests(unittest.TestCase):
    def test_batch_matches_legacy_on_fixture(self) -> None:
        async def run() -> None:
            async with async_playwright() as pw:
                firefox = await pw.firefox.launch()
                page = await firefox.new_page(viewport={"width": 1280, "height": 800})
                await page.set_content(FIXTURE)
                await page.wait_for_timeout(200)
                legacy, batched = await _both_paths(page)
                # Second page WITHOUT file inputs: pins the mirrored legacy
                # quirk where empty_file_upload_present is unconditionally
                # True (the flag is computed from the function object).
                await page.set_content("<html><body><input name='x'></body></html>")
                await page.wait_for_timeout(100)
                legacy2, batched2 = await _both_paths(page)
                await firefox.close()

                self.assertEqual(_semantic(legacy), _semantic(batched))
                self.assertEqual(_semantic(legacy2), _semantic(batched2))

        asyncio.run(run())

    def test_determinism_across_repeats(self) -> None:
        async def run() -> None:
            async with async_playwright() as pw:
                firefox = await pw.firefox.launch()
                page = await firefox.new_page(viewport={"width": 1280, "height": 800})
                await page.set_content(FIXTURE)
                first = await inspect_page_capabilities_batched(page, timeout_s=5.0)
                second = await inspect_page_capabilities_batched(page, timeout_s=5.0)
                await firefox.close()
                self.assertEqual(_semantic(first), _semantic(second))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
