"""Cache-first capability rider pinning tests (OPT-DEC-010 H1).

The binding acceptance criterion: `_filter_tools` consumes a capability
object on EVERY turn, including dedupe-skipped ones. The signature-keyed
cache must serve those turns at zero browser scans — one inspect call for N
same-revision turns, a fresh scan when the observation signature changes.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage

from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.browser_observation import BrowserCapabilities


class _FakeObservation:
    def __init__(self, revision: int, signature: str) -> None:
        self.revision = revision
        self.signature = signature
        self.evidence = ""

    def bounded_render(self, budget_chars: int = 0) -> str:
        return ""

    def render(self) -> str:
        return f"fake observation {self.revision}"


class _CountingEvidenceStore:
    def save(self, observation: Any) -> Any:
        return None


class _CountingBrowser:
    def __init__(self) -> None:
        self.inspect_calls = 0
        self.pending_atomic_upload_target = ""
        self._signature: str | None = "sig-a"
        self._observation_accesses = 0

    def set_signature(self, signature: str | None) -> None:
        # Real revisions change when mutations happen between model calls.
        self._signature = signature

    @property
    def current_observation(self) -> _FakeObservation | None:
        if self._signature is None:
            return None
        return _FakeObservation(revision=1 + self.inspect_calls, signature=self._signature)

    async def inspect_capabilities(self) -> BrowserCapabilities:
        self.inspect_calls += 1
        return BrowserCapabilities(editable_controls_visible=True)


def _run_turns(browser: Any, turns: int) -> list[ModelRequest[Any]]:
    # The middleware instance persists across a run's model calls; the cache
    # lives on it, so all turns must share one instance.
    middleware = CapabilityContextMiddleware(  # type: ignore[arg-type]
        browser,
        evidence_store=_CountingEvidenceStore(),
    )
    captured: list[ModelRequest[Any]] = []

    async def run() -> None:
        for _index in range(turns):
            request = ModelRequest(
                model=GenericFakeChatModel(messages=iter(["ok"])),
                messages=[HumanMessage(content="go")],
                tools=[],
            )

            async def handler(req: ModelRequest[Any]) -> ModelResponse[Any]:
                captured.append(req)
                return ModelResponse(result=[])

            await middleware.awrap_model_call(request, handler)

    asyncio.run(run())
    return captured


class CacheFirstRiderTests(unittest.TestCase):
    def test_same_signature_turns_scan_once(self) -> None:
        browser = _CountingBrowser()

        _run_turns(browser, 3)

        # Three turns consumed capabilities; only the first scanned.
        assert browser.inspect_calls == 1

    def test_changed_signature_rescans(self) -> None:
        browser = _CountingBrowser()

        _run_turns(browser, 2)
        browser.set_signature("sig-b")
        _run_turns(browser, 1)

        assert browser.inspect_calls == 2


if __name__ == "__main__":
    unittest.main()
