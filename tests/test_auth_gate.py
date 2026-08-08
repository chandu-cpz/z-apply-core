from __future__ import annotations

import unittest

from z_apply_core.graph import HARD_BLOCK_AUTH_STATUSES, _route_after_auth
from z_apply_core.nodes.auth_blocked import auth_blocked
from z_apply_core.state import RunState


class AuthGateRoutingTests(unittest.TestCase):
    def test_hard_block_statuses_route_to_auth_blocked(self) -> None:
        self.assertEqual(HARD_BLOCK_AUTH_STATUSES, frozenset({"failed", "blocked"}))
        for status in ("failed", "blocked"):
            with self.subTest(status=status):
                self.assertEqual(
                    _route_after_auth(RunState(auth_status=status)),
                    "auth_blocked",
                )

    def test_authenticated_and_not_verified_route_to_orchestrator(self) -> None:
        for status in ("authenticated", "not_verified", ""):
            with self.subTest(status=status):
                self.assertEqual(
                    _route_after_auth(RunState(auth_status=status)),
                    "orchestrator",
                )

    def test_missing_auth_status_routes_to_orchestrator(self) -> None:
        self.assertEqual(_route_after_auth(RunState()), "orchestrator")


class AuthBlockedNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_node_terminates_with_summary(self) -> None:
        result = await auth_blocked(
            RunState(
                auth_status="failed",
                auth_summary="Simplify login could not be restored.",
                auth_model_id="provider/model",
                snapshot="<snapshot>",
            ),
            {},
        )
        self.assertEqual(result["run_status"], "blocked")
        self.assertIn("authentication failed", result["orchestrator_summary"])
        self.assertIn("Simplify login could not be restored.", result["orchestrator_summary"])
        self.assertEqual(result["model_id"], "provider/model")
        self.assertEqual(result["snapshot"], "<snapshot>")

    async def test_blocked_node_defaults_missing_verdict(self) -> None:
        result = await auth_blocked(RunState(), {})
        self.assertEqual(result["run_status"], "blocked")
        self.assertIn("authentication failed", result["orchestrator_summary"])


if __name__ == "__main__":
    unittest.main()
