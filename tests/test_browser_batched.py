from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.tools import ToolException
from pydantic import ValidationError

from z_apply_core.agents.no_progress_guard import _target_refs
from z_apply_core.agents.specialists.submission_reviewer import REVIEWER_BROWSER_TOOLS
from z_apply_core.browser_session import (
    BrowserSession,
    BrowserToolExecutionError,
    SubmitControlKind,
)
from z_apply_core.browser_tools import (
    AUTHENTICATION_SPECIALIST_BROWSER_TOOLS,
    INITIAL_AGENT_BROWSER_TOOLS,
    MAX_BATCH_STEPS,
    BrowserBatchArgs,
    make_batched_tool,
)
from z_apply_core.nodes.setup_browser import _agent_browser_tools


class BatchSchemaTests(unittest.TestCase):
    def _tool_call(self, steps: object, call_id: str = "tc1") -> dict[str, object]:
        return {
            "id": call_id,
            "name": "browser_batched",
            "args": {"steps": steps},
            "type": "tool_call",
        }

    def test_rejects_unknown_action(self) -> None:
        with self.assertRaises(ValidationError):
            BrowserBatchArgs.model_validate({"steps": [{"action": "nope"}]})

    def test_rejects_empty_and_oversized_step_lists(self) -> None:
        with self.assertRaises(ValidationError):
            BrowserBatchArgs.model_validate({"steps": []})
        with self.assertRaises(ValidationError):
            BrowserBatchArgs.model_validate(
                {"steps": [{"action": "snapshot"} for _ in range(MAX_BATCH_STEPS + 1)]}
            )

    def test_rejects_missing_required_fields_and_extra_keys(self) -> None:
        with self.assertRaises(ValidationError):
            BrowserBatchArgs.model_validate({"steps": [{"action": "click"}]})
        with self.assertRaises(ValidationError):
            BrowserBatchArgs.model_validate(
                {"steps": [{"action": "click", "target": "e1", "bogus": 1}]}
            )
        with self.assertRaises(ValidationError):
            BrowserBatchArgs.model_validate(
                {
                    "steps": [
                        {
                            "action": "fill_form",
                            "fields": [{"target": "e1", "value": "x", "bogus": 1}],
                        }
                    ]
                }
            )
        with self.assertRaises(ValidationError):
            BrowserBatchArgs.model_validate(
                {
                    "steps": [
                        {
                            "action": "fill_form",
                            "fields": [
                                {"target": "e1", "value": "x", "element": "textbox"}
                            ],
                        }
                    ]
                }
            )
        filled = BrowserBatchArgs.model_validate(
            {
                "steps": [
                    {
                        "action": "fill_form",
                        "fields": [
                            {"target": "e1", "type": "textbox", "value": "x"},
                            {"target": "e2", "name": "Age", "value": "30"},
                        ],
                    }
                ]
            }
        )
        dumped = [step.model_dump() for step in filled.steps]
        self.assertEqual(dumped[0]["fields"][0]["type"], "textbox")
        self.assertEqual(dumped[0]["fields"][1]["name"], "Age")

    def test_stray_ref_folds_into_target_at_step_and_field_level(self) -> None:
        model = BrowserBatchArgs.model_validate(
            {
                "steps": [
                    {"action": "click", "ref": "[ref=e12]"},
                    {
                        "action": "fill_form",
                        "fields": [{"ref": "ref=e34", "value": "x"}],
                    },
                ]
            }
        )
        steps = [step.model_dump() for step in model.steps]
        self.assertEqual(steps[0]["target"], "[ref=e12]")
        self.assertEqual(steps[1]["fields"][0]["target"], "ref=e34")
        from z_apply_core.browser_tools import normalize_browser_arguments

        normalized = [normalize_browser_arguments(step) for step in steps]
        self.assertEqual(normalized[0]["target"], "e12")
        self.assertEqual(normalized[1]["fields"][0]["target"], "e34")

    def test_empty_fill_form_step_is_rejected(self) -> None:
        from z_apply_core.browser_tools import normalize_browser_arguments

        with self.assertRaisesRegex(ToolException, "empty fields list"):
            normalize_browser_arguments({"action": "fill_form", "fields": []})

    def test_blank_snapshot_step_target_is_rejected(self) -> None:
        from z_apply_core.browser_tools import normalize_browser_arguments

        for blank in ("", "   "):
            with self.assertRaisesRegex(ToolException, "empty target"):
                normalize_browser_arguments({"action": "snapshot", "target": blank})

    def test_snapshot_step_without_target_stays_full_page(self) -> None:
        from z_apply_core.browser_tools import normalize_browser_arguments

        normalized = normalize_browser_arguments({"action": "snapshot", "depth": 2})

        self.assertNotIn("target", normalized)
        self.assertEqual(normalized["depth"], 2)

    def test_valid_steps_and_standalone_args_pass_through(self) -> None:
        from z_apply_core.browser_tools import normalize_browser_arguments

        filled = normalize_browser_arguments(
            {"action": "fill_form", "fields": [{"target": "e12", "value": "x"}]}
        )
        self.assertEqual(filled["fields"], [{"target": "e12", "value": "x"}])

        targeted = normalize_browser_arguments({"action": "snapshot", "target": "e90"})
        self.assertEqual(targeted["target"], "e90")

        # Standalone tool calls carry no ``action`` key and keep today's
        # strip-empty behavior instead of the batched-step rejections.
        standalone = normalize_browser_arguments({"fields": [], "target": ""})
        self.assertEqual(standalone, {"fields": []})

    def test_schema_carries_one_compact_argument(self) -> None:
        schema = BrowserBatchArgs.model_json_schema()
        self.assertEqual(list(schema["properties"]), ["steps"])
        self.assertLess(len(json.dumps(schema)), 8_000)
        self.assertNotIn("browser_navigate", json.dumps(schema))


class BrowserBatchExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _session(
        self,
        *,
        snapshot: str = "snapshot: form open",
    ) -> tuple[BrowserSession, AsyncMock]:
        async def call_tool(name: str, arguments: object | None = None, *, meta: object = None) -> str:
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
            run_id="batched",
            backend=backend,
            tools=[],
            owns_backend=False,
        )
        session._last_snapshot = snapshot
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]
        return session, backend.call_tool

    async def test_steps_dispatch_in_order_with_single_final_snapshot(self) -> None:
        session, call_tool = self._session()
        await session.run_action_batch(
            [
                {"action": "navigate", "url": "https://example.com"},
                {"action": "click", "target": "e12"},
                {"action": "type", "target": "e34", "text": "hi"},
                {"action": "fill_form", "fields": [{"target": "e56", "value": "x"}]},
                {"action": "select_option", "target": "e78", "values": ["a"]},
                {"action": "wait_for", "text": "done"},
                {"action": "handle_dialog", "accept": True},
                {"action": "snapshot", "target": "e99"},
            ]
        )
        names = [call.args[0] for call in call_tool.await_args_list]
        self.assertEqual(
            names,
            [
                "browser_navigate",
                "browser_click",
                "browser_type",
                "browser_fill_form",
                "browser_select_option",
                "browser_wait_for",
                "browser_handle_dialog",
                "browser_snapshot",
                "browser_snapshot",
            ],
        )
        self.assertEqual(call_tool.await_args_list[-1].args[1], {"target": "html"})
        self.assertEqual(session.last_observation_revision, 1)

    async def test_success_receipt_renders_markers_and_stamps_revision(self) -> None:
        session, _call_tool = self._session()
        tool = make_batched_tool(
            session.run_action_batch,
            revision_provider=lambda: session.last_observation_revision,
        )
        result = await tool.ainvoke(
            {
                "id": "tc1",
                "name": "browser_batched",
                "args": {
                    "steps": [
                        {"action": "click", "target": "e1"},
                        {"action": "type", "target": "e2", "text": "x"},
                    ]
                },
                "type": "tool_call",
            }
        )
        self.assertEqual(result.status, "success")
        self.assertIn("BROWSER BATCH RECEIPT", result.content)
        self.assertIn("steps: 2 planned, 2 ok", result.content)
        self.assertIn("- 1 click e1 ok", result.content)
        self.assertIn("- 2 type e2 ok", result.content)
        self.assertIn("after_revision: 1", result.content)
        self.assertEqual(result.additional_kwargs["browser_revision"], 1)
        self.assertEqual(session.last_action_receipt.tool, "browser_batched")

    async def test_stopped_batch_returns_error_status_without_stamp(self) -> None:
        async def call_tool(name: str, arguments: object | None = None, *, meta: object = None) -> str:
            del meta
            if name == "browser_click" and arguments and arguments.get("target") == "eBAD":
                raise RuntimeError("actionability: element detached")
            if name == "browser_snapshot":
                return "snapshot: v2"
            return "ok"

        backend = SimpleNamespace(
            call_tool=AsyncMock(side_effect=call_tool),
            _ensure_tab=AsyncMock(return_value=SimpleNamespace(page=MagicMock())),
        )
        session = BrowserSession(None, run_id="batched-stop", backend=backend, tools=[], owns_backend=False)
        session._last_snapshot = "snapshot: v1"
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]
        tool = make_batched_tool(
            session.run_action_batch,
            revision_provider=lambda: session.last_observation_revision,
        )
        result = await tool.ainvoke(
            {
                "id": "tc1",
                "name": "browser_batched",
                "args": {
                    "steps": [
                        {"action": "click", "target": "e1"},
                        {"action": "click", "target": "eBAD"},
                        {"action": "click", "target": "e2"},
                    ]
                },
                "type": "tool_call",
            }
        )
        self.assertEqual(result.status, "error")
        self.assertIn("stopped_at: 2 (click)", result.content)
        self.assertIn("steps: 3 planned, 1 ok", result.content)
        self.assertIn("element detached", result.content)
        self.assertNotIn("browser_revision", result.additional_kwargs or {})

    async def test_file_upload_trigger_in_batch_sets_pending_target(self) -> None:
        session, call_tool = self._session()
        session._is_file_upload_trigger = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda arguments: arguments.get("target") == "eUpload"
        )
        with self.assertRaisesRegex(BrowserToolExecutionError, "Native file chooser click rejected"):
            await session.run_action_batch(
                [
                    {"action": "click", "target": "e1"},
                    {"action": "click", "target": "eUpload"},
                ]
            )
        self.assertEqual(session.pending_atomic_upload_target, "eUpload")
        self.assertEqual(call_tool.await_count, 2)

    async def test_submit_click_in_batch_requires_armed_approval(self) -> None:
        session, call_tool = self._session()
        session.activate_submission_guard()
        session._classify_submit_control = AsyncMock(  # type: ignore[method-assign]
            return_value=SubmitControlKind.FORM_SUBMIT
        )
        with self.assertRaisesRegex(BrowserToolExecutionError, "submission is locked"):
            await session.run_action_batch([{"action": "click", "target": "eSubmit"}])
        self.assertEqual(call_tool.await_count, 1)

    async def test_guarded_submit_click_consumes_approval(self) -> None:
        session, call_tool = self._session()
        session.activate_submission_guard()
        session.set_submit_approval(True)
        session._classify_submit_control = AsyncMock(  # type: ignore[method-assign]
            return_value=SubmitControlKind.FORM_SUBMIT
        )
        session._page_is_alive = AsyncMock(return_value=True)  # type: ignore[method-assign]
        await session.run_action_batch([{"action": "click", "target": "eSubmit"}])
        self.assertTrue(session.submission_consumed())
        self.assertEqual(call_tool.await_count, 2)

    async def test_guarded_type_submit_consumes_approval(self) -> None:
        session, call_tool = self._session()
        session.activate_submission_guard()
        session.set_submit_approval(True)
        session._page_is_alive = AsyncMock(return_value=True)  # type: ignore[method-assign]
        await session.run_action_batch(
            [{"action": "type", "target": "eSubmit", "text": "\n", "submit": True}]
        )
        self.assertTrue(session.submission_consumed())
        self.assertEqual(call_tool.await_count, 2)

    async def test_mid_batch_failure_preserves_earlier_steps_and_raises(self) -> None:
        async def call_tool(name: str, arguments: object | None = None, *, meta: object = None) -> str:
            del meta
            if name == "browser_click" and arguments and arguments.get("target") == "eBAD":
                raise RuntimeError("boom")
            if name == "browser_snapshot":
                return "snapshot: v2"
            return "ok"

        backend = SimpleNamespace(
            call_tool=AsyncMock(side_effect=call_tool),
            _ensure_tab=AsyncMock(return_value=SimpleNamespace(page=MagicMock())),
        )
        session = BrowserSession(None, run_id="batched-stop2", backend=backend, tools=[], owns_backend=False)
        session._last_snapshot = "snapshot: v1"
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]
        with self.assertRaisesRegex(BrowserToolExecutionError, "stopped_at: 2"):
            await session.run_action_batch(
                [
                    {"action": "click", "target": "e1"},
                    {"action": "click", "target": "eBAD"},
                ]
            )
        self.assertEqual(backend.call_tool.await_count, 3)
        self.assertEqual(session.last_action_receipt.tool, "browser_batched")


class BatchWiringTests(unittest.TestCase):
    def _session_tools(self) -> list[object]:
        backend = SimpleNamespace(
            call_tool=AsyncMock(return_value="ok"),
            _ensure_tab=AsyncMock(return_value=SimpleNamespace(page=MagicMock())),
        )
        session = BrowserSession(None, run_id="wiring", backend=backend, tools=[], owns_backend=False)
        return _agent_browser_tools(session)

    def test_batch_mode_exposes_batched_tool_without_legacy_mutations(self) -> None:
        with patch("z_apply_core.nodes.setup_browser.load_settings") as settings:
            settings.return_value = SimpleNamespace(browser_batch_tools=True, default_resume_path=Path("/tmp/resume.pdf"))
            names = {getattr(tool, "name", "") for tool in self._session_tools()}
        self.assertIn("browser_batched", names)
        self.assertNotIn("browser_navigate", names)
        self.assertNotIn("browser_click", names)
        self.assertNotIn("browser_evaluate", names)
        self.assertIn("browser_observe", names)
        self.assertIn("browser_click_upload", names)

    def test_legacy_mode_restores_legacy_mutations(self) -> None:
        with patch("z_apply_core.nodes.setup_browser.load_settings") as settings:
            settings.return_value = SimpleNamespace(browser_batch_tools=False, default_resume_path=Path("/tmp/resume.pdf"))
            names = {getattr(tool, "name", "") for tool in self._session_tools()}
        self.assertNotIn("browser_batched", names)
        self.assertIn("browser_observe", names)
        self.assertIn("browser_click_upload", names)

    def test_specialist_filters_keep_batched_out_of_auth_and_reviewer(self) -> None:
        self.assertNotIn("browser_batched", AUTHENTICATION_SPECIALIST_BROWSER_TOOLS)
        self.assertNotIn("browser_batched", REVIEWER_BROWSER_TOOLS)

    def test_legacy_tool_sets_do_not_name_the_batch(self) -> None:
        self.assertNotIn("browser_batched", INITIAL_AGENT_BROWSER_TOOLS)


class BatchNoProgressGuardTests(unittest.TestCase):
    def test_target_refs_walks_nested_batch_steps(self) -> None:
        refs = _target_refs(
            {
                "steps": [
                    {"action": "click", "target": "e1"},
                    {"action": "fill_form", "fields": [{"target": "e2"}, {"target": "e3"}]},
                    {"action": "type", "target": "e4"},
                ]
            }
        )
        self.assertEqual(refs, frozenset({"e1", "e2", "e3", "e4"}))


if __name__ == "__main__":
    unittest.main()
