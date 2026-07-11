from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage
from nim_router import NimRouter

from z_apply_core.agents.application_outcome import (
    MAX_OUTCOME_VERDICT_ATTEMPTS,
    OutcomeDecision,
    evaluate_application_outcome,
    fresh_snapshot,
)
from z_apply_core.agents.orchestrator import run_orchestrator
from z_apply_core.stream_events import V3RunResult


class FakeWorker:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def astream_events(self, value: dict[str, Any], **_kwargs: Any) -> object:
        self.inputs.append(value)
        return object()


class ApplicationOutcomeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_premature_worker_stop_resumes_without_untrusted_assistant_prose(
        self,
    ) -> None:
        worker = FakeWorker()
        router = NimRouter()
        selection = SimpleNamespace(
            llm=MagicMock(),
            info=SimpleNamespace(id="provider/model"),
        )
        first_output = {
            "messages": [AIMessage(content="I will inspect the form and upload the resume next.")],
            "todos": [{"content": "Upload resume", "status": "pending"}],
            "files": {"notes.md": "worker state"},
            "_z_apply_tool_trace": [],
        }
        second_output = {
            "messages": [AIMessage(content="Resume uploaded and form review completed.")],
            "todos": [{"content": "Upload resume", "status": "completed"}],
            "files": {"notes.md": "worker state"},
            "_z_apply_tool_trace": [
                {
                    "source": "BrowserSpecialist",
                    "tool_name": "browser_file_upload",
                    "completed": True,
                    "output": "Chandrakanth-V-Resume.pdf",
                }
            ],
        }

        with (
            patch.object(router, "lease", AsyncMock(return_value=selection)),
            patch.object(router, "cooldown_model") as cooldown_model,
            patch(
                "z_apply_core.agents.orchestrator.create_deep_agent",
                return_value=worker,
            ),
            patch(
                "z_apply_core.agents.orchestrator.build_specialists",
                AsyncMock(return_value=[]),
            ),
            patch(
                "z_apply_core.agents.orchestrator.consume_deepagent_stream",
                AsyncMock(
                    side_effect=[
                        V3RunResult(output=first_output),
                        V3RunResult(output=second_output),
                    ]
                ),
            ),
            patch(
                "z_apply_core.agents.orchestrator.fresh_snapshot",
                AsyncMock(side_effect=["form snapshot", "uploaded snapshot"]),
            ),
            patch(
                "z_apply_core.agents.orchestrator.evaluate_application_outcome",
                AsyncMock(
                    side_effect=[
                        OutcomeDecision(
                            "needs_revision",
                            "The form opened, but no upload operation was recorded.",
                            "Upload the configured resume through BrowserSpecialist.",
                        ),
                        OutcomeDecision("satisfied", "Application is review-ready."),
                    ]
                ),
            ) as evaluator,
        ):
            result = await run_orchestrator(
                job_url="https://example.test/job",
                task="Prepare the application for review.",
                snapshot="job page",
                browser_tools=[],
                config={},
                router=router,
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.summary, "Application is review-ready.")
        self.assertEqual(len(worker.inputs), 2)
        resumed = worker.inputs[1]
        self.assertEqual(resumed["todos"], first_output["todos"])
        self.assertEqual(resumed["files"], first_output["files"])
        self.assertEqual(len(resumed["messages"]), 1)
        self.assertIsInstance(resumed["messages"][-1], HumanMessage)
        self.assertIn("Upload the configured resume", resumed["messages"][-1].content)
        self.assertNotIn("I will inspect the form", resumed["messages"][-1].content)
        cooldown_model.assert_called_once_with("provider/model", 60.0)
        final_journal = evaluator.await_args_list[-1].kwargs["tool_journal"]
        self.assertEqual(
            [entry["tool_name"] for entry in final_journal],
            ["browser_file_upload"],
        )

    async def test_outcome_contract_failure_stops_the_run_without_retrying_the_worker(
        self,
    ) -> None:
        worker = FakeWorker()
        router = NimRouter()
        selection = SimpleNamespace(
            llm=MagicMock(),
            info=SimpleNamespace(id="provider/model"),
        )
        worker_output = V3RunResult(output={"messages": [], "_z_apply_tool_trace": []})

        with (
            patch.object(router, "lease", AsyncMock(return_value=selection)),
            patch(
                "z_apply_core.agents.orchestrator.create_deep_agent",
                return_value=worker,
            ),
            patch(
                "z_apply_core.agents.orchestrator.build_specialists",
                AsyncMock(return_value=[]),
            ),
            patch(
                "z_apply_core.agents.orchestrator.consume_deepagent_stream",
                AsyncMock(return_value=worker_output),
            ),
            patch(
                "z_apply_core.agents.orchestrator.fresh_snapshot",
                AsyncMock(return_value="form snapshot"),
            ),
            patch(
                "z_apply_core.agents.orchestrator.evaluate_application_outcome",
                AsyncMock(
                    return_value=OutcomeDecision(
                        "failed",
                        "The evaluator did not record a typed decision.",
                    )
                ),
            ),
        ):
            result = await run_orchestrator(
                job_url="https://example.test/job",
                task="Prepare the application for review.",
                snapshot="job page",
                browser_tools=[],
                config={},
                router=router,
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(len(worker.inputs), 1)


class OutcomeEvaluatorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_snapshot_preserves_current_modal_failure(self) -> None:
        snapshot_tool = MagicMock()
        snapshot_tool.name = "browser_snapshot"
        snapshot_tool.ainvoke = AsyncMock(
            side_effect=RuntimeError("browser_snapshot does not handle modal state")
        )

        snapshot = await fresh_snapshot([snapshot_tool], "stale form snapshot")

        self.assertIn("Snapshot unavailable", snapshot)
        self.assertIn("modal state", snapshot)
        self.assertNotEqual(snapshot, "stale form snapshot")

    async def test_missing_transition_is_retried_then_reported_as_failed(self) -> None:
        router = NimRouter()
        selection = SimpleNamespace(llm=MagicMock(), info=SimpleNamespace(id="provider/model"))
        evaluator = FakeWorker()

        with (
            patch.object(router, "lease", AsyncMock(return_value=selection)),
            patch(
                "z_apply_core.agents.application_outcome.create_deep_agent",
                return_value=evaluator,
            ),
            patch(
                "z_apply_core.agents.application_outcome.consume_deepagent_stream",
                AsyncMock(return_value=V3RunResult(output={})),
            ) as consume,
        ):
            decision = await evaluate_application_outcome(
                task="Prepare the application for review.",
                output={"messages": []},
                tool_journal=[],
                snapshot="form snapshot",
                router=router,
                sink=None,
            )

        self.assertEqual(decision.status, "failed")
        self.assertIn("typed decision", decision.explanation)
        self.assertEqual(len(evaluator.inputs), MAX_OUTCOME_VERDICT_ATTEMPTS)
        self.assertEqual(consume.await_count, MAX_OUTCOME_VERDICT_ATTEMPTS)

    async def test_recorded_transition_returns_without_a_duplicate_attempt(self) -> None:
        router = NimRouter()
        selection = SimpleNamespace(llm=MagicMock(), info=SimpleNamespace(id="provider/model"))
        evaluator = FakeWorker()
        captured_tools: list[Any] = []

        def make_evaluator(**kwargs: Any) -> FakeWorker:
            captured_tools.extend(kwargs["tools"])
            return evaluator

        async def record_transition(*_args: Any, **_kwargs: Any) -> V3RunResult:
            transition = next(
                tool for tool in captured_tools if tool.name == "outcome_needs_revision"
            )
            await transition.ainvoke(
                {"feedback": "Resume is not attached.", "next_action": "Upload the resume."}
            )
            return V3RunResult(output={})

        with (
            patch.object(router, "lease", AsyncMock(return_value=selection)),
            patch(
                "z_apply_core.agents.application_outcome.create_deep_agent",
                side_effect=make_evaluator,
            ),
            patch(
                "z_apply_core.agents.application_outcome.consume_deepagent_stream",
                side_effect=record_transition,
            ) as consume,
        ):
            decision = await evaluate_application_outcome(
                task="Prepare the application for review.",
                output={"messages": []},
                tool_journal=[],
                snapshot="form snapshot",
                router=router,
                sink=None,
            )

        self.assertEqual(decision.status, "needs_revision")
        self.assertEqual(decision.next_action, "Upload the resume.")
        self.assertEqual(consume.await_count, 1)


if __name__ == "__main__":
    unittest.main()
