"""PROP-005 S2: provenance ledger + evaluate bypass block."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from z_apply_core.browser_session import BrowserSession, BrowserToolExecutionError
from z_apply_core.browser_value_provenance import (
    ProvenanceEntry,
    first_form_write,
    value_digest,
)


class WritePatternTests(unittest.TestCase):
    def test_blocks_assignment_writes(self) -> None:
        for body in (
            "el.value = 'x'",
            "el.value='x'",
            "input.value += 'more'",
            "el.checked = true",
            "t.textContent = 'invented'",
            "t.innerText = 'x'",
            "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,'x')",
            "HTMLTextAreaElement.prototype['value'].set.call(t,'x')",
            "document.execCommand('insertText', false, 'x')",
            "el.setAttribute('value', 'stale')",
        ):
            with self.subTest(body=body):
                self.assertIsNotNone(first_form_write(body), body)

    def test_allows_reads_and_non_control_writes(self) -> None:
        for body in (
            "return el.value",
            "if (el.value === 'x') return true;",
            "return el.value.length",
            "const v = el.checked; return v;",
            "el.style.width = '10px'",
            "el.dataset.marker = 'seen'",
            "console.log(el.textContent)",
            "return document.querySelectorAll('input').length",
            "",
        ):
            with self.subTest(body=body):
                self.assertIsNone(first_form_write(body), body)

    def test_digest_is_stable_and_short(self) -> None:
        self.assertEqual(value_digest("₹12,00,000"), value_digest("₹12,00,000"))
        self.assertEqual(len(value_digest("anything")), 12)
        self.assertNotEqual(value_digest("a"), value_digest("b"))


class ProvenanceEntryTests(unittest.TestCase):
    def test_entries_for_type_fill_select(self) -> None:
        from z_apply_core.browser_value_provenance import provenance_entries_for

        assert provenance_entries_for("browser_type", {"target": "e105", "text": "hi"}) == (
            ProvenanceEntry("e105", value_digest("hi"), "browser_type"),
        )
        fill = provenance_entries_for(
            "browser_fill_form", {"fields": [{"target": "e1", "value": "a"}, {"target": "e2"}]}
        )
        assert fill == (ProvenanceEntry("e1", value_digest("a"), "browser_fill_form"),)
        select = provenance_entries_for(
            "browser_select_option", {"target": "e9", "values": ["India"]}
        )
        assert select == (ProvenanceEntry("e9", value_digest("India"), "browser_select_option"),)
        assert provenance_entries_for("browser_click", {"target": "e1"}) == ()

    def test_entry_render_carries_hash_not_value(self) -> None:
        entry = ProvenanceEntry("e105", value_digest("secret-compensation"), "browser_type")
        rendered = entry.render()
        self.assertIn("e105", rendered)
        self.assertIn(value_digest("secret-compensation"), rendered)
        self.assertNotIn("secret-compensation", rendered)


class _SessionHarness:
    """Fake-backend BrowserSession mirroring test_browser_batched's harness."""

    @staticmethod
    def build(snapshot: str = "snapshot: form open") -> tuple[BrowserSession, AsyncMock]:
        async def call_tool(
            name: str, arguments: object | None = None, *, meta: object = None
        ) -> str:
            del meta
            if name == "browser_snapshot":
                return snapshot
            return "ok"

        tab = SimpleNamespace(page=MagicMock())
        backend = SimpleNamespace(
            call_tool=AsyncMock(side_effect=call_tool),
            _ensure_tab=AsyncMock(return_value=tab),
        )
        session = BrowserSession(
            None,
            run_id="provenance",
            backend=backend,
            tools=[],
            owns_backend=False,
        )
        session._last_snapshot = snapshot
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]
        session._classify_submit_control = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(  # not a real enum use in these paths
                value="not_submit"
            )
        )
        return session, backend.call_tool


class EvaluateBypassBlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_evaluate_write_rejected_before_backend(self) -> None:
        session, backend = _SessionHarness.build()

        with self.assertRaises(BrowserToolExecutionError) as caught:
            await session.call_tool(
                "browser_evaluate",
                {"function": "() => { document.querySelector('#sal').value = '₹12L'; }"},
            )
        self.assertIn("writes form-control values", str(caught.exception))
        # The backend never saw the call.
        for name, _args in backend.call_tool.call_args_list:
            assert name != "browser_evaluate"

    async def test_batched_evaluate_write_rejected_and_stops_step(self) -> None:
        session, _backend = _SessionHarness.build()

        # A stopped batch is a failed script: surfaced as a contained tool error
        # (legacy behavior for failing batches). The rejection message carries
        # the reason, and the honest step's provenance rides along in the same
        # receipt text.
        with self.assertRaises(BrowserToolExecutionError) as caught:
            await session.run_action_batch(
                [
                    {"action": "type", "target": "e34", "text": "honest"},
                    {
                        "action": "evaluate",
                        "function": "() => { el.textContent = 'fabricated'; }",
                    },
                ]
            )

        message = str(caught.exception)
        self.assertIn("writes form-control values", message)
        self.assertIn("via browser_type", message)
        self.assertIn(f"sha256={value_digest('honest')}", message)

    async def test_read_only_evaluate_passes(self) -> None:
        session, _backend = _SessionHarness.build()

        result = await session.call_tool(
            "browser_evaluate",
            {"function": "() => document.querySelector('#sal').value.length"},
        )

        self.assertEqual(result, "ok")


class ReceiptProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_snapshot_receipt_records_written_controls(self) -> None:
        session, _backend = _SessionHarness.build()

        receipt = await session.call_tool_with_inline_snapshot(
            "browser_type", {"target": "e105", "text": "honest value"}
        )

        self.assertIn("written_controls", receipt)
        self.assertIn("e105", receipt)
        self.assertIn("via browser_type", receipt)
        self.assertIn(f"sha256={value_digest('honest value')}", receipt)
        self.assertNotIn("honest value\n", receipt.split("arguments:")[0])

    async def test_batch_receipt_accumulates_per_step_provenance(self) -> None:
        session, _backend = _SessionHarness.build()

        receipt = await session.run_action_batch(
            [
                {"action": "type", "target": "e34", "text": "Hyderabad"},
                {"action": "fill_form", "fields": [{"target": "e56", "value": "India"}]},
                {"action": "select_option", "target": "e78", "values": ["2"]},
            ]
        )

        self.assertIn("via browser_type", receipt)
        self.assertIn("via browser_fill_form", receipt)
        self.assertIn("via browser_select_option", receipt)
        self.assertIn(f"sha256={value_digest('Hyderabad')}", receipt)
        self.assertIn(f"sha256={value_digest('India')}", receipt)

    async def test_click_receipt_has_no_written_section(self) -> None:
        session, _backend = _SessionHarness.build()

        receipt = await session.call_tool_with_inline_snapshot("browser_click", {"target": "e12"})

        self.assertNotIn("written_controls", receipt)


if __name__ == "__main__":
    unittest.main()
