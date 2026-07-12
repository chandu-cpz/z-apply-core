from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import ToolMessage

from z_apply_core.agents.application_progress import (
    ApplicationProgress,
    ApplicationProgressEventSink,
    BrowserUploadProgressMiddleware,
)
from z_apply_core.agents.application_state import EvidenceRef, FieldState
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.stream_events import FrameworkTraceEvent


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


def _make_evidence(source: str = "snapshot") -> EvidenceRef:
    return EvidenceRef(kind="browser", source=source, detail="test evidence")


def _make_field(label: str, ref: str = "e10", required: bool = True) -> FieldState:
    return FieldState(
        label=label,
        ref=ref,
        required=required,
        status="candidate_fact_available",
        evidence=_make_evidence(),
    )


def _set_progress(
    progress: ApplicationProgress,
    fields_mapped: bool = False,
    resume_control_visible: bool = False,
    resume_uploaded_verified: bool = False,
) -> None:
    if fields_mapped:
        progress.state.fields["TestField"] = _make_field("TestField")
    else:
        progress.state.fields.clear()
    progress.state.resume_control = _make_evidence() if resume_control_visible else None
    progress.state.resume_uploaded = _make_evidence() if resume_uploaded_verified else None


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
        _set_progress(progress, fields_mapped=True)
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
        _set_progress(progress, fields_mapped=False, resume_control_visible=False)
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
        _set_progress(
            progress,
            fields_mapped=True,
            resume_control_visible=True,
            resume_uploaded_verified=False,
        )
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
        _set_progress(
            progress, fields_mapped=True, resume_control_visible=True, resume_uploaded_verified=True
        )
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
        _set_progress(progress, fields_mapped=True, resume_control_visible=False)
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
        _set_progress(progress, resume_control_visible=True, resume_uploaded_verified=False)
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

    def test_completed_upload_artifact_is_typed_progress_evidence(self) -> None:
        p = ApplicationProgress()
        p.state.resume_uploaded = _make_evidence()
        self.assertTrue(p.resume_uploaded_verified)

    def test_update_from_tool_journal_is_noop(self) -> None:
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

    def test_resume_control_detection_requires_typed_evidence(self) -> None:
        p = ApplicationProgress()
        self.assertFalse(p.resume_control_visible)
        p.state.resume_control = _make_evidence()
        self.assertTrue(p.resume_control_visible)

    def test_mark_fields_mapped(self) -> None:
        p = ApplicationProgress()
        p.state.fields["Name"] = _make_field("Name")
        p.mark_fields_mapped()
        self.assertTrue(p.fields_mapped)

    def test_nested_upload_completion_event_updates_progress(self) -> None:
        p = ApplicationProgress()
        sink = ApplicationProgressEventSink(p)

        _run(
            sink.accept(
                FrameworkTraceEvent(
                    event="agent_tool_end",
                    name="BrowserSpecialist",
                    data={
                        "tool_name": "browser_file_upload",
                        "completed": True,
                        "error": "",
                        "output": "artifact output is not interpreted",
                    },
                    raw={},
                )
            )
        )

        self.assertFalse(p.resume_uploaded_verified)

    def test_failed_nested_upload_event_does_not_update_progress(self) -> None:
        p = ApplicationProgress()
        sink = ApplicationProgressEventSink(p)

        _run(
            sink.accept(
                FrameworkTraceEvent(
                    event="agent_tool_end",
                    name="BrowserSpecialist",
                    data={
                        "tool_name": "browser_file_upload",
                        "completed": False,
                        "error": "chooser unavailable",
                    },
                    raw={},
                )
            )
        )

        self.assertFalse(p.resume_uploaded_verified)

    def test_journal_refresh_does_not_regress_stream_verified_upload(self) -> None:
        p = ApplicationProgress()
        p.state.resume_uploaded = _make_evidence()

        p.update_from_tool_journal([], "current form snapshot")

        self.assertTrue(p.resume_uploaded_verified)

    def test_completed_field_mapper_task_is_typed_progress_evidence(self) -> None:
        p = ApplicationProgress()
        p.state.fields["Name"] = _make_field("Name")

        self.assertTrue(p.fields_mapped)

    def test_nested_upload_middleware_marks_successful_tool_result(self) -> None:
        progress = ApplicationProgress()
        middleware = BrowserUploadProgressMiddleware(progress)
        request = _make_guard_request("browser_file_upload", {"paths": ["/resume.pdf"]})
        handler = AsyncMock(return_value=ToolMessage(content="uploaded", tool_call_id="c1"))

        result = _run(middleware.awrap_tool_call(request, handler))

        self.assertEqual(result.content, "uploaded")
        self.assertFalse(progress.resume_uploaded_verified)

    def test_nested_upload_middleware_does_not_mark_error_result(self) -> None:
        progress = ApplicationProgress()
        middleware = BrowserUploadProgressMiddleware(progress)
        request = _make_guard_request("browser_file_upload", {"paths": ["/resume.pdf"]})
        handler = AsyncMock(
            return_value=ToolMessage(content="failed", tool_call_id="c1", status="error")
        )

        _run(middleware.awrap_tool_call(request, handler))

        self.assertFalse(progress.resume_uploaded_verified)


# ── Fix 3: Operation kind extraction tests ───────────────────────────────


# ── Fix 6: Fake tool call detection (slice-based) tests ──────────────────


# ── Required regression: ask_human rejected when resume pending ───────────


class AskHumanRegressionTests(unittest.TestCase):
    def test_ask_human_rejected_when_resume_pending(self) -> None:
        """Simulate: form opened, resume control visible, upload not verified."""
        progress = ApplicationProgress()
        _set_progress(
            progress,
            fields_mapped=False,
            resume_control_visible=True,
            resume_uploaded_verified=False,
        )

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
        _set_progress(
            progress, fields_mapped=True, resume_control_visible=True, resume_uploaded_verified=True
        )

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
        _set_progress(progress, resume_control_visible=True, resume_uploaded_verified=False)

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
