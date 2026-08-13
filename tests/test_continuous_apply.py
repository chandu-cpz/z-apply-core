"""Tests for scripts/continuous_apply.py (runner state-machine helpers)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_CORE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_CORE / "scripts"))

import continuous_apply as ca  # noqa: E402


class RetryableFailureTests(unittest.TestCase):
    def test_failed_with_zero_calls_is_retryable(self) -> None:
        # A failed run that never made a model call never got a browser
        # (pool capacity / launch) — safe to re-queue.
        self.assertTrue(
            ca._is_retryable_failure(
                {"status": "failed", "calls": 0, "terminal_detail": "Core execution failed."}
            )
        )

    def test_failed_without_calls_field_is_retryable(self) -> None:
        # Entries recorded before ledger draining existed have no calls key.
        self.assertTrue(ca._is_retryable_failure({"status": "failed"}))

    def test_failed_with_calls_is_not_retryable(self) -> None:
        # The agent ran and may have submitted; re-queue only with --force.
        self.assertFalse(
            ca._is_retryable_failure({"status": "failed", "calls": 10, "terminal_detail": "x"})
        )

    def test_blocked_and_interrupted_are_not_retryable(self) -> None:
        self.assertFalse(ca._is_retryable_failure({"status": "blocked", "calls": 16}))
        self.assertFalse(ca._is_retryable_failure({"status": "interrupted", "calls": 13}))


class ActiveRunsTests(unittest.TestCase):
    def test_active_runs_excludes_only_terminal(self) -> None:
        rows = [
            {"status": "queued", "job_url": "https://j/1"},
            {"status": "starting", "job_url": "https://j/2"},
            {"status": "running", "job_url": "https://j/3"},
            {"status": "waiting_human", "job_url": "https://j/4"},
            {"status": "terminal", "job_url": "https://j/5"},
        ]
        with patch.object(ca, "_http_json", return_value=rows):
            active = ca._active_runs("http://x")
            urls = ca._active_urls("http://x")
        self.assertEqual(len(active), 4)
        self.assertEqual(urls, {"https://j/1", "https://j/2", "https://j/3", "https://j/4"})


class DrainFinishedTests(unittest.TestCase):
    def test_drain_sets_status_calls_and_cost_once(self) -> None:
        state = {"attempted": {"r1": {"url": "https://j/x"}}, "total_cost_usd": 0.0}
        terminal_rows = [
            {"id": "r1", "status": "terminal", "outcome": "blocked", "summary": "phone field rejected"},
        ]

        def fake_http(method: str, url: str, payload=None) -> object:
            if url.endswith("/r1/calls"):
                return {"totals": {"cost_usd": 0.0123, "calls": 27}}
            return terminal_rows

        with patch.object(ca, "_http_json", side_effect=fake_http):
            total = ca._drain_finished("http://x", state)

        self.assertEqual(state["attempted"]["r1"]["status"], "blocked")
        self.assertEqual(state["attempted"]["r1"]["calls"], 27)
        self.assertAlmostEqual(state["attempted"]["r1"]["cost_usd"], 0.0123)
        self.assertAlmostEqual(total, 0.0123)

    def test_drain_skips_entry_on_transient_ledger_error(self) -> None:
        state = {"attempted": {"r1": {"url": "https://j/x"}}, "total_cost_usd": 0.0}

        def fake_http(method: str, url: str, payload=None) -> object:
            if url.endswith("/r1/calls"):
                raise RuntimeError("backend down")
            return [{"id": "r1", "status": "terminal", "outcome": "failed", "summary": "x"}]

        with patch.object(ca, "_http_json", side_effect=fake_http):
            total = ca._drain_finished("http://x", state)

        # Entry stays incomplete so a later drain re-fetches; no cost folded.
        self.assertNotIn("cost_usd", state["attempted"]["r1"])
        self.assertEqual(total, 0.0)


if __name__ == "__main__":
    unittest.main()
