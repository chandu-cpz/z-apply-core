"""submission truth events, classifier widening, reconciliation.

P1: every submit attempt emits submission.failed/completed at the EXECUTOR
layer (recovery turns bypass the turn pipeline); precondition failures NEVER
consume the armed approval; narration alone can never produce
SUBMITTED_VERIFIED (outcome gate in service).
P3: form-less typeless JS submits classify as form_submit only when position
(last visible enabled button in container) AND vocabulary name agree.
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import async_playwright

from z_apply_core.browser_targeting import classify_submit_control


def _page_with(buttons_html: str) -> str:
    return f"<html><body><div id='actions'>{buttons_html}</div></body></html>"


FORMLESS_SUBMIT = "<button>Submit application</button>"


async def _classify_page(page: Any, buttons_html: str) -> tuple[str, Any]:
    await page.set_content(_page_with(buttons_html))
    await page.wait_for_timeout(50)
    target = page.locator("#actions > button").last
    return await classify_submit_control(page, target)


def test_widening_accepts_formless_typeless_submit() -> None:
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            kind = await _classify_page(page, FORMLESS_SUBMIT)
            await browser.close()
            return kind

    kind, _control = asyncio.run(run())
    assert kind == "form_submit"


def test_negative_fixture_back_button_is_excluded() -> None:
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            kind = await _classify_page(page, "<button>Back</button>")
            await browser.close()
            return kind

    kind, _control = asyncio.run(run())
    assert kind == "not_submit"


def test_widening_requires_vocabulary_and_last_position() -> None:
    class _Result:
        def __init__(self, html: str) -> None:
            self.html = html

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            draft_kind, _ = await _classify_page(page, "<button>Save draft</button>")
            # Back first then Submit: Submit is last → accepted despite Back.
            both_kind, _ = await _classify_page(
                page, "<button>Back</button><button>Submit</button>"
            )
            # Submit first then Cancel: vocabulary button is NOT last → rejected.
            first_kind, _ = await _classify_page(
                page, "<button>Submit</button><button>Cancel</button>"
            )
            await browser.close()
            return draft_kind, both_kind, first_kind

    draft_kind, both_kind, first_kind = asyncio.run(run())
    assert draft_kind == "not_submit", "no submit-intent word"
    assert both_kind == "form_submit", "Submit last + vocabulary"
    assert first_kind == "not_submit", "vocabulary button not last → rejected"


class _FakeSubmissionGuard:
    def __init__(self) -> None:
        self.armed = True
        self.consumed = False

    def require_armed(self) -> None:
        if self.consumed or not self.armed:
            raise ValueError("submission approval is not armed")

    def consume(self) -> None:
        self.consumed = True

    def is_consumed(self) -> bool:
        return self.consumed


class _Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def accept(self, event) -> None:
        self.events.append((event.event, event.data))


def test_failed_resolution_emits_failure_and_keeps_approval_armed(monkeypatch: Any) -> None:
    """Precondition failures never consume the armed approval (attempt-13 pin)."""

    from z_apply_core.browser_session import BrowserSession

    session = object.__new__(BrowserSession)
    session._submission = _FakeSubmissionGuard()
    sink = _Sink()
    session._event_sink = sink

    async def failing_resolve():
        raise RuntimeError("No enabled form submit control is visible.")

    monkeypatch.setattr(session, "resolve_submit_control_target", failing_resolve)

    with __import__("pytest").raises(RuntimeError):
        asyncio.run(session.submit_approved_application())
    asyncio.run(_flush())

    assert session._submission.consumed is False, (
        "precondition failure must NOT consume the armed approval"
    )
    kinds = [kind for kind, _ in sink.events]
    assert kinds == ["submission_failed"]
    assert "No enabled form submit control" in sink.events[0][1]["error"]


async def _flush() -> None:
    await asyncio.sleep(0.05)


def test_completed_click_emits_submission_completed(monkeypatch: Any) -> None:

    from z_apply_core.browser_session import BrowserSession

    session = object.__new__(BrowserSession)
    session._submission = _FakeSubmissionGuard()

    async def resolving_target():
        return "e4967"

    async def click(name, arguments):
        return "clicked"

    class _Obs:
        def __str__(self) -> str:
            return "success page evidence"

    async def observe():
        return "success page evidence"

    monkeypatch.setattr(session, "resolve_submit_control_target", resolving_target)
    monkeypatch.setattr(session, "call_tool", click)
    monkeypatch.setattr(session, "observe", observe)
    sink = _Sink()
    session._event_sink = sink

    result = asyncio.run(session.submit_approved_application())
    asyncio.run(_flush())

    assert "success page evidence" in result
    kinds = [kind for kind, _ in sink.events]
    assert kinds == ["submission_completed"]


def test_reconciliation_fires_only_on_single_vocabulary_match(monkeypatch: Any) -> None:
    """P4: zero native candidates + exactly ONE enabled vocab button → use it."""
    from unittest.mock import MagicMock

    from z_apply_core import browser_session as bs_module
    from z_apply_core.browser_session import BrowserSession

    session = object.__new__(BrowserSession)
    session._submission = _FakeSubmissionGuard()

    calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        assert name == "browser_snapshot"
        return (
            '- button "Cancel" [ref=e1]\n'
            '- button "Submit" [ref=e4967] [cursor=pointer]:\n'
            '- link "Privacy" [ref=e3]\n'
        )

    monkeypatch.setattr(session, "call_tool", call_tool)
    monkeypatch.setattr(bs_module, "classify_submit_control", _fake_classify)

    class _FakeLocator:
        def __init__(self, *, n: int) -> None:
            self._n = n

        async def count(self) -> int:
            return self._n

    # Zero native candidates.
    monkeypatch.setattr(
        "z_apply_core.browser_targeting.SUBMIT_SELECTOR", "nothing-matches", raising=False
    )
    empty_selector = MagicMock()
    empty_selector.count.return_value = 0  # type: ignore[method-assign]

    class _FakePage:
        def __init__(self) -> None:
            self.role_calls = 0

        def locator(self, selector):
            assert selector == "nothing-matches"
            return _FakeLocator(n=0)

        def get_by_role(self, role, name=None):
            self.role_calls += 1
            return _RoleCandidates()

    class _RoleCandidates:
        async def count(self) -> int:
            return 1

        def nth(self, index: int):
            return _EnabledButton()

    class _EnabledButton:
        async def is_visible(self) -> bool:
            return True

        async def is_enabled(self) -> bool:
            return True

    page = _FakePage()
    ref = asyncio.run(session._reconcile_js_submit_target(page))
    assert ref == "e4967"

    # Two vocabulary buttons in fresh evidence → refuse to guess.
    async def ambiguous_call_tool(name, arguments):
        return (
            '- button "Submit" [ref=eA] [cursor=pointer]\n'
            '- button "Send" [ref=eB] [cursor=pointer]\n'
        )

    monkeypatch.setattr(session, "call_tool", ambiguous_call_tool)
    ref2 = asyncio.run(session._reconcile_js_submit_target(_FakePage()))
    assert ref2 is None


async def _fake_classify(page, target):  # noqa: ANN001
    return "form_submit", target
