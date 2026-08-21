from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.context.cost_estimate import estimate_cost, format_cost


class CostEstimateTests(unittest.TestCase):
    def test_million_token_call_at_groq_defaults(self) -> None:
        estimate = estimate_cost("groq", 1_000_000, 1_000_000)

        self.assertAlmostEqual(estimate.usd, 0.55, places=4)
        self.assertEqual(estimate.cache_read_tokens, 0)

    def test_opencodego_uses_official_deepseek_v4_flash_rates(self) -> None:
        estimate = estimate_cost("opencodego", 1_000_000, 1_000_000)

        self.assertAlmostEqual(estimate.usd, 0.42, places=4)
        self.assertEqual(format_cost(estimate), "$0.4200")

    def test_cache_read_tokens_bill_at_cache_rate(self) -> None:
        estimate = estimate_cost(
            "opencodego",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )

        # 1M cached input at $0.0028 + 1M output at $0.28
        self.assertAlmostEqual(estimate.usd, 0.2828, places=4)
        self.assertEqual(estimate.cache_read_tokens, 1_000_000)

    def test_cache_read_never_exceeds_input(self) -> None:
        estimate = estimate_cost(
            "opencodego", input_tokens=100, output_tokens=0, cache_read_tokens=500
        )

        self.assertEqual(estimate.cache_read_tokens, 100)

    def test_env_rates_override_defaults(self) -> None:
        with patch.dict(
            "os.environ",
            {"OPENCODEGO_PRICE_IN": "1.0", "OPENCODEGO_PRICE_OUT": "2.0"},
            clear=False,
        ):
            estimate = estimate_cost("opencodego", 1_000_000, 500_000)

        self.assertAlmostEqual(estimate.usd, 2.0, places=4)


class CallLedgerTests(unittest.TestCase):
    def test_records_entries_with_sequence_and_totals(self) -> None:
        ledger = RunCallLedger(job_url="https://example.test/job")

        ledger.record(
            agent="orchestrator",
            model_id="deepseek-v4-flash",
            provider="opencodego",
            input_tokens=100,
            output_tokens=20,
            ttft_ms=100,
            duration_ms=500,
        )
        ledger.record(
            agent="AnswerWriter",
            model_id="deepseek-v4-flash",
            provider="opencodego",
            input_tokens=400,
            output_tokens=60,
            ttft_ms=90,
            duration_ms=600,
        )

        self.assertEqual(ledger.job_url, "https://example.test/job")
        self.assertEqual(ledger.call_count, 2)
        self.assertEqual([entry.sequence for entry in ledger.entries], [1, 2])
        self.assertEqual(
            [entry.agent for entry in ledger.entries], ["orchestrator", "AnswerWriter"]
        )
        self.assertEqual(ledger.total_input_tokens, 500)
        self.assertEqual(ledger.total_output_tokens, 80)
        self.assertAlmostEqual(
            ledger.total_cost_usd,
            (500 * 0.14 + 80 * 0.28) / 1_000_000,
            places=5,
        )

    def test_cache_read_tokens_flow_into_totals(self) -> None:
        ledger = RunCallLedger()

        ledger.record(
            agent="orchestrator",
            model_id="deepseek-v4-flash",
            provider="opencodego",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )

        self.assertEqual(ledger.total_input_tokens, 1_000_000)
        self.assertEqual(ledger.total_cache_read_tokens, 1_000_000)
        self.assertEqual(ledger.total_new_input_tokens, 0)
        self.assertAlmostEqual(ledger.total_cost_usd, 0.2828, places=4)

    def test_resuming_thread_input_totals_do_not_recount_context(self) -> None:
        # A deep-agent thread re-sends its full conversation on every call;
        # the provider serves the repeated prefix from cache. The raw input
        # total sums the same context per call, so the non-recounted total
        # must be input minus cache reads.
        ledger = RunCallLedger()

        ledger.record(
            agent="orchestrator",
            model_id="deepseek-v4-flash",
            provider="opencodego",
            input_tokens=10_000,
            output_tokens=200,
        )
        ledger.record(
            agent="orchestrator",
            model_id="deepseek-v4-flash",
            provider="opencodego",
            input_tokens=15_000,
            output_tokens=200,
            cache_read_tokens=10_000,
        )
        ledger.record(
            agent="orchestrator",
            model_id="deepseek-v4-flash",
            provider="opencodego",
            input_tokens=20_000,
            output_tokens=200,
            cache_read_tokens=15_000,
        )

        self.assertEqual(ledger.total_input_tokens, 45_000)
        self.assertEqual(ledger.total_cache_read_tokens, 25_000)
        self.assertEqual(ledger.total_new_input_tokens, 20_000)
        self.assertEqual([e.new_input_tokens for e in ledger.entries], [10_000, 5_000, 5_000])

    def test_new_input_tokens_clamped_at_zero(self) -> None:
        ledger = RunCallLedger()

        ledger.record(
            agent="orchestrator",
            model_id="deepseek-v4-flash",
            provider="opencodego",
            input_tokens=100,
            output_tokens=0,
            cache_read_tokens=500,
        )

        self.assertEqual(ledger.entries[0].new_input_tokens, 0)
        self.assertEqual(ledger.total_new_input_tokens, 0)
        self.assertEqual(ledger.total_input_tokens, 100)


if __name__ == "__main__":
    unittest.main()


class LedgerPersistenceTests(unittest.TestCase):
    def test_write_history_appends_jsonl_and_run_copy(self) -> None:
        from tempfile import TemporaryDirectory

        from z_apply_core.config import CORE_ROOT  # noqa: F401  (import check)

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            ledger = RunCallLedger(job_url="https://example.test/job")
            ledger.record(
                agent="orchestrator",
                model_id="deepseek-v4-flash",
                provider="opencodego",
                input_tokens=1000,
                output_tokens=100,
                cache_read_tokens=500,
            )
            history, run_copy = ledger.write_history(base, run_id="run-1", status="completed")

            self.assertTrue(history.exists())
            self.assertIsNotNone(run_copy)
            assert run_copy is not None
            self.assertTrue(run_copy.exists())

            import json

            record = json.loads(history.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(record["run_id"], "run-1")
            self.assertEqual(record["job_url"], "https://example.test/job")
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["totals"]["calls"], 1)
            self.assertEqual(record["totals"]["input_tokens"], 1000)
            self.assertEqual(record["totals"]["cache_read_tokens"], 500)
            self.assertEqual(record["totals"]["new_input_tokens"], 500)
            self.assertEqual(record["calls"][0]["new_input_tokens"], 500)
            self.assertAlmostEqual(record["totals"]["cost_usd"], 0.0000994, places=6)

    def test_terminal_reason_flows_into_record(self) -> None:
        import json
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            ledger = RunCallLedger(job_url="https://example.test/job")
            ledger.record(
                agent="orchestrator",
                model_id="deepseek-v4-flash",
                provider="opencodego",
                input_tokens=100,
                output_tokens=10,
            )
            ledger.set_terminal_reason(
                "stuck_loop: the application loop repeatedly made no progress"
            )
            history, _run_copy = ledger.write_history(base, run_id="run-1", status="blocked")

            record = json.loads(history.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(record["status"], "blocked")
            self.assertEqual(
                record["terminal_reason"],
                "stuck_loop: the application loop repeatedly made no progress",
            )

    def test_gateway_cost_wins_over_estimate(self) -> None:
        ledger = RunCallLedger()

        ledger.record(
            agent="orchestrator",
            model_id="deepseek-v4-flash",
            provider="opencodego",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            gateway_cost_usd=0.1234,
        )

        entry = ledger.entries[0]
        self.assertEqual(entry.gateway_cost_usd, 0.1234)
        self.assertAlmostEqual(entry.cost.usd, 0.1234, places=6)
        self.assertAlmostEqual(ledger.total_cost_usd, 0.1234, places=6)

    def test_zero_gateway_cost_is_authoritative(self) -> None:
        ledger = RunCallLedger()

        ledger.record(
            agent="orchestrator",
            model_id="mimo-v2.5",
            provider="opencodego",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            gateway_cost_usd=0.0,
        )

        # The OpenCode Go gateway reports cost "0" on subscription-covered
        # requests; that is authoritative, not a signal to fall back to the
        # rate-card estimate.
        self.assertEqual(ledger.entries[0].gateway_cost_usd, 0.0)
        self.assertEqual(ledger.entries[0].cost.usd, 0.0)
        self.assertEqual(ledger.total_cost_usd, 0.0)

    def test_missing_gateway_cost_falls_back_to_estimate(self) -> None:
        ledger = RunCallLedger()

        ledger.record(
            agent="orchestrator",
            model_id="deepseek-v4-flash",
            provider="opencodego",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )

        self.assertAlmostEqual(ledger.entries[0].cost.usd, 0.42, places=4)


if __name__ == "__main__":
    unittest.main()
