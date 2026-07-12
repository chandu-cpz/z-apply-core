from __future__ import annotations

import unittest

from z_apply_core.agents.no_progress_guard import NoProgressCircuitOpen
from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.agents.retry_policy import should_retry_model_error
from z_apply_core.agents.terminal_guard import TerminalDecisionRecorded


class ShouldRetryModelErrorTests(unittest.TestCase):
    def test_transport_error_retried(self) -> None:
        self.assertTrue(should_retry_model_error(ConnectionError("timeout")))

    def test_generic_exception_retried(self) -> None:
        self.assertTrue(should_retry_model_error(RuntimeError("oops")))

    def test_tool_protocol_violation_not_retried(self) -> None:
        self.assertFalse(should_retry_model_error(ToolProtocolViolation("prose tool call")))

    def test_no_progress_circuit_open_not_retried(self) -> None:
        self.assertFalse(should_retry_model_error(NoProgressCircuitOpen("stuck")))

    def test_terminal_decision_recorded_not_retried(self) -> None:
        self.assertFalse(should_retry_model_error(TerminalDecisionRecorded("submit approved")))


if __name__ == "__main__":
    unittest.main()
