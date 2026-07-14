from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from z_apply_core.browser_readiness import BrowserFormReadiness
from z_apply_core.browser_session import BrowserSession


class BrowserReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_returns_only_browser_reported_constraint_failures(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "blockers": [
                        {
                            "control": "Email",
                            "reasons": ["required control is empty", "Email is required"],
                        }
                    ],
                    "submit_controls": [{"control": "Apply now", "disabled": True}],
                }
            )
        )
        session = object.__new__(BrowserSession)
        session._backend = SimpleNamespace(
            _ensure_tab=AsyncMock(return_value=SimpleNamespace(page=page))
        )
        session._lease = None

        evidence = await session.inspect_form_readiness()

        self.assertEqual(evidence.blockers[0].control, "Email")
        self.assertEqual(
            evidence.blockers[0].reasons,
            ("required control is empty", "Email is required"),
        )
        self.assertTrue(evidence.submit_controls[0].disabled)
        page.evaluate.assert_awaited_once()

    def test_malformed_model_like_claims_are_not_accepted_as_browser_blockers(self) -> None:
        evidence = BrowserFormReadiness.from_browser_payload(
            {"blockers": ["the dates look wrong"], "submit_controls": []}
        )

        self.assertEqual(evidence.blockers, ())


if __name__ == "__main__":
    unittest.main()
