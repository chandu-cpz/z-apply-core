from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from z_apply_core.agents.application_progress import ApplicationProgress
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.orchestrator import detect_fake_tool_calls
from z_apply_core.agents.post_task_verification import extract_operation_kind


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_guard_request(
    tool_name: str,
    args: dict[str, Any],
    call_id: str = "c1",
) -> MagicMock:
    request = MagicMock()
    request.tool_call = {"name": tool_name, "args": args, "id": call_id}
    return request


# ── Fix 1: HumanEscalationGuardMiddleware tests ─────────────────────────


class HumanEscalationGuardTests(unittest.TestCase):
    def test_allows_human_challenge(self) -> None:
        progress = ApplicationProgress()
        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        args = {"question": "captcha?", "reason": "human_challenge"}
        req = _make_guard_request("ask_human", args)
        _run(guard.awrap_tool_call(req, handler))
        handler.assert_called_once()

    def test_rejects_invalid_reason(self) -> None:
        progress = ApplicationProgress()
        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock()
        req = _make_guard_request("ask_human", {"question": "hey", "reason": "please_help"})
        result = _run(guard.awrap_tool_call(req, handler))
        handler.assert_not_called()
        self.assertIn("invalid reason", result.content)

    def test_rejects_no_field_label(self) -> None:
        progress = ApplicationProgress()
        progress.fields_mapped = True
        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock()
        req = _make_guard_request(
            "ask_human",
            {"question": "what is your name?", "reason": "missing_candidate_fact"},
        )
        result = _run(guard.awrap_tool_call(req, handler))
        handler.assert_not_called()
        self.assertIn("no specific field", result.content)

    def test_rejects_when_fields_not_mapped(self) -> None:
        progress = ApplicationProgress()
        progress.fields_mapped = False
        progress.resume_control_visible = False
        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock()
        req = _make_guard_request(
            "ask_human",
            {
                "question": "what is your DOB?",
                "reason": "missing_candidate_fact",
                "field_label": "Date of Birth",
                "field_evidence": "empty field visible",
            },
        )
        result = _run(guard.awrap_tool_call(req, handler))
        handler.assert_not_called()
        self.assertIn("not been mapped", result.content)

    def test_rejects_when_resume_upload_pending(self) -> None:
        progress = ApplicationProgress()
        progress.fields_mapped = True
        progress.resume_control_visible = True
        progress.resume_uploaded_verified = False
        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock()
        req = _make_guard_request(
            "ask_human",
            {
                "question": "what is your email?",
                "reason": "missing_candidate_fact",
                "field_label": "Email",
                "field_evidence": "empty field visible",
            },
        )
        result = _run(guard.awrap_tool_call(req, handler))
        handler.assert_not_called()
        self.assertIn("resume has not been verified", result.content)

    def test_allows_when_resume_uploaded_and_fields_mapped(self) -> None:
        progress = ApplicationProgress()
        progress.fields_mapped = True
        progress.resume_control_visible = True
        progress.resume_uploaded_verified = True
        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        req = _make_guard_request(
            "ask_human",
            {
                "question": "what is your DOB?",
                "reason": "missing_candidate_fact",
                "field_label": "Date of Birth",
                "field_evidence": "empty field visible",
            },
        )
        _run(guard.awrap_tool_call(req, handler))
        handler.assert_called_once()

    def test_allows_when_no_resume_control(self) -> None:
        progress = ApplicationProgress()
        progress.fields_mapped = True
        progress.resume_control_visible = False
        progress.resume_uploaded_verified = False
        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        req = _make_guard_request(
            "ask_human",
            {
                "question": "what is your phone?",
                "reason": "missing_candidate_fact",
                "field_label": "Phone",
                "field_evidence": "empty field visible",
            },
        )
        _run(guard.awrap_tool_call(req, handler))
        handler.assert_called_once()

    def test_passes_through_non_ask_human(self) -> None:
        progress = ApplicationProgress()
        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        req = _make_guard_request("browser_click", {"target": "e112"})
        _run(guard.awrap_tool_call(req, handler))
        handler.assert_called_once()

    def test_request_submit_approval_unaffected(self) -> None:
        progress = ApplicationProgress()
        progress.resume_control_visible = True
        progress.resume_uploaded_verified = False
        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        req = _make_guard_request("request_submit_approval", {"final_review": "all good"})
        _run(guard.awrap_tool_call(req, handler))
        handler.assert_called_once()


# ── Fix 2: ApplicationProgress tests ────────────────────────────────────


class ApplicationProgressTests(unittest.TestCase):
    def test_initial_state(self) -> None:
        p = ApplicationProgress()
        self.assertFalse(p.form_open_verified)
        self.assertFalse(p.resume_control_visible)
        self.assertFalse(p.resume_uploaded_verified)
        self.assertFalse(p.fields_mapped)

    def test_update_from_tool_journal_upload_verified(self) -> None:
        p = ApplicationProgress()
        journal = [
            {
                "tool_name": "browser_file_upload",
                "completed": True,
                "error": "",
                "output": "Uploaded Resume.pdf",
            },
        ]
        p.update_from_tool_journal(journal, "resume upload section visible")
        self.assertTrue(p.resume_uploaded_verified)

    def test_update_from_tool_journal_upload_not_verified(self) -> None:
        p = ApplicationProgress()
        journal = [
            {
                "tool_name": "browser_click",
                "completed": True,
                "error": "",
                "output": "[Snapshot](path.yml)",
            },
        ]
        p.update_from_tool_journal(journal, "resume upload control visible")
        self.assertFalse(p.resume_uploaded_verified)

    def test_resume_control_detection_from_snapshot(self) -> None:
        p = ApplicationProgress()
        p.update_from_tool_journal([], "Upload Resume / CV section visible")
        self.assertTrue(p.resume_control_visible)

    def test_mark_fields_mapped(self) -> None:
        p = ApplicationProgress()
        p.mark_fields_mapped()
        self.assertTrue(p.fields_mapped)


# ── Fix 3: Operation kind extraction tests ───────────────────────────────


class OperationKindTests(unittest.TestCase):
    def test_extracts_application_entry(self) -> None:
        desc = (
            "OPERATION KIND: application_entry\n"
            "OPERATION: Click Apply\n"
            "SUCCESS CONDITION: form visible"
        )
        self.assertEqual(extract_operation_kind(desc), "application_entry")

    def test_extracts_resume_upload(self) -> None:
        desc = (
            "OPERATION KIND: resume_upload\n"
            "OPERATION: Upload resume\n"
            "SUCCESS CONDITION: file uploaded"
        )
        self.assertEqual(extract_operation_kind(desc), "resume_upload")

    def test_extracts_fill_fields(self) -> None:
        desc = (
            "OPERATION KIND: fill_fields\n"
            "OPERATION: Fill name field\n"
            "SUCCESS CONDITION: field populated"
        )
        self.assertEqual(extract_operation_kind(desc), "fill_fields")

    def test_extracts_inspect(self) -> None:
        desc = "OPERATION KIND: inspect\nOPERATION: Observe page\nSUCCESS CONDITION: snapshot taken"
        self.assertEqual(extract_operation_kind(desc), "inspect")

    def test_returns_empty_when_missing(self) -> None:
        desc = "OPERATION: Click Apply\nSUCCESS CONDITION: form visible"
        self.assertEqual(extract_operation_kind(desc), "")


# ── Fix 6: Fake tool call detection (slice-based) tests ──────────────────


class DetectFakeToolCallsSliceTests(unittest.TestCase):
    def test_fake_detected_when_no_real_calls_in_slice(self) -> None:
        output = {"messages": [MagicMock(content='Use browser_click(target="e200") now')]}
        result = detect_fake_tool_calls([], output)
        self.assertIsNotNone(result)
        self.assertIn("agent_protocol_error", result)

    def test_fake_not_detected_when_real_call_in_slice(self) -> None:
        journal = [
            {"tool_name": "browser_click", "completed": True, "error": ""},
        ]
        output = {"messages": [MagicMock(content='Use browser_click(target="e200") now')]}
        result = detect_fake_tool_calls(journal, output)
        self.assertIsNone(result)

    def test_fake_detected_with_json_shape_no_calls(self) -> None:
        output = {"messages": [MagicMock(content='{"text": "Upload Resume"}')]}
        result = detect_fake_tool_calls([], output)
        self.assertIsNotNone(result)
        self.assertIn("agent_protocol_error", result)

    def test_no_fake_when_no_patterns(self) -> None:
        output = {"messages": [MagicMock(content="Task completed successfully.")]}
        result = detect_fake_tool_calls([], output)
        self.assertIsNone(result)

    def test_earlier_real_call_does_not_mask_later_fake(self) -> None:
        """Regression: iteration 1 has real click, iteration 2 fakes. Must detect."""
        iter2_journal: list[dict[str, Any]] = []
        output = {"messages": [MagicMock(content='Now use browser_click(target="e200")')]}
        result = detect_fake_tool_calls(iter2_journal, output)
        self.assertIsNotNone(result)
        self.assertIn("agent_protocol_error", result)


# ── Required regression: ask_human rejected when resume pending ───────────


class AskHumanRegressionTests(unittest.TestCase):
    def test_ask_human_rejected_when_resume_pending(self) -> None:
        """Simulate: form opened, resume control visible, upload not verified."""
        progress = ApplicationProgress()
        progress.form_open_verified = True
        progress.resume_control_visible = True
        progress.resume_uploaded_verified = False
        progress.fields_mapped = False

        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock()
        req = _make_guard_request(
            "ask_human",
            {
                "question": "Please provide your name, email, phone, DOB",
                "reason": "missing_candidate_fact",
                "field_label": "Name",
                "field_evidence": "empty fields visible",
            },
        )
        result = _run(guard.awrap_tool_call(req, handler))
        handler.assert_not_called()
        self.assertIn("resume has not been verified", result.content)
        self.assertIn("Complete and verify resume upload first", result.content)

    def test_ask_human_allowed_after_resume_verified_and_fields_mapped(self) -> None:
        """Simulate: resume uploaded, fields mapped, genuinely missing candidate fact."""
        progress = ApplicationProgress()
        progress.form_open_verified = True
        progress.resume_control_visible = True
        progress.resume_uploaded_verified = True
        progress.fields_mapped = True

        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        req = _make_guard_request(
            "ask_human",
            {
                "question": "What is your date of birth?",
                "reason": "missing_candidate_fact",
                "field_label": "Date of Birth",
                "field_evidence": "DOB field is required but empty",
            },
        )
        _run(guard.awrap_tool_call(req, handler))
        handler.assert_called_once()

    def test_ask_human_allowed_for_human_challenge(self) -> None:
        """Simulate: genuine CAPTCHA/OTP dependency."""
        progress = ApplicationProgress()
        progress.form_open_verified = True
        progress.resume_control_visible = True
        progress.resume_uploaded_verified = False

        guard = HumanEscalationGuardMiddleware(progress)
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        req = _make_guard_request(
            "ask_human",
            {
                "question": "Please solve the CAPTCHA",
                "reason": "human_challenge",
            },
        )
        _run(guard.awrap_tool_call(req, handler))
        handler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
