from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from z_apply_core.agents.action_order import OrchestratorActionOrderMiddleware
from z_apply_core.agents.specialists.answer_writer import CandidateFieldAnswer
from z_apply_core.browser_observation import BrowserCapabilities


def response(name: str, args: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        result=[AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])]
    )


def answer_result(
    *, label: str = "Name", target: str = "e1", value: str = "Chandrakanth"
) -> Command:
    answer = CandidateFieldAnswer(
        outcome="resolved",
        field_label=label,
        target=target,
        value=value,
    )
    return Command(
        update={"messages": [ToolMessage(answer.model_dump_json(), tool_call_id="task")]}
    )


class ActionOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_entry_requires_one_answer_writer_result_per_field(self) -> None:
        browser = SimpleNamespace(required_file_upload_pending=AsyncMock(return_value=False))
        middleware = OrchestratorActionOrderMiddleware(browser)
        unsafe_fill = response(
            "browser_fill_form",
            {"fields": [{"target": "e1", "value": "plausible@example.com"}]},
        )
        answer = response(
            "task",
            {"subagent_type": "AnswerWriter", "description": "Email"},
        )
        handler = AsyncMock(side_effect=[unsafe_fill, answer])
        request = SimpleNamespace(messages=[], override=lambda **values: SimpleNamespace(**values))

        self.assertIs(await middleware.awrap_model_call(request, handler), answer)

    async def test_answer_writer_result_must_be_consumed_by_browser_mutation(self) -> None:
        browser = SimpleNamespace(required_file_upload_pending=AsyncMock(return_value=False))
        middleware = OrchestratorActionOrderMiddleware(browser)
        first = response("task", {"subagent_type": "AnswerWriter", "description": "Name"})
        wrong_fill = response(
            "browser_fill_form",
            {"fields": [{"target": "e2", "value": "Kanamarlapudi"}]},
        )
        fill = response(
            "browser_fill_form",
            {"fields": [{"target": "e1", "value": "Chandrakanth"}]},
        )
        handler = AsyncMock(side_effect=[first, wrong_fill, fill])
        request = SimpleNamespace(messages=[], override=lambda **values: SimpleNamespace(**values))

        self.assertIs(await middleware.awrap_model_call(request, handler), first)
        task_request = SimpleNamespace(
            tool_call={
                "name": "task",
                "args": {"subagent_type": "AnswerWriter", "description": "Name"},
                "id": "task",
            }
        )
        await middleware.awrap_tool_call(
            task_request,
            AsyncMock(return_value=answer_result()),
        )
        self.assertIs(await middleware.awrap_model_call(request, handler), fill)
        self.assertEqual(handler.await_count, 3)

    async def test_empty_answer_writer_result_does_not_lock_action_order(self) -> None:
        browser = SimpleNamespace(required_file_upload_pending=AsyncMock(return_value=False))
        middleware = OrchestratorActionOrderMiddleware(browser)
        task_request = SimpleNamespace(
            tool_call={
                "name": "task",
                "args": {"subagent_type": "AnswerWriter", "description": "Source"},
                "id": "task",
            }
        )
        await middleware.awrap_tool_call(
            task_request,
            AsyncMock(
                return_value=Command(update={"messages": [ToolMessage("", tool_call_id="task")]})
            ),
        )
        next_task = response(
            "task",
            {"subagent_type": "AnswerWriter", "description": "Retry source"},
        )
        handler = AsyncMock(return_value=next_task)
        request = SimpleNamespace(messages=[], override=lambda **values: SimpleNamespace(**values))

        self.assertIs(await middleware.awrap_model_call(request, handler), next_task)
        self.assertEqual(handler.await_count, 1)

    async def test_required_file_upload_precedes_answer_writer(self) -> None:
        browser = SimpleNamespace(required_file_upload_pending=AsyncMock(return_value=True))
        middleware = OrchestratorActionOrderMiddleware(browser)
        task = response("task", {"subagent_type": "AnswerWriter", "description": "Name"})
        upload = response("browser_click_upload", {"target": "e1", "paths": ["resume.pdf"]})
        handler = AsyncMock(side_effect=[task, upload])
        request = SimpleNamespace(messages=[], override=lambda **values: SimpleNamespace(**values))

        self.assertIs(await middleware.awrap_model_call(request, handler), upload)

    async def test_auth_gate_rejects_answer_writer_and_requires_auth_specialist(self) -> None:
        browser = SimpleNamespace(
            inspect_capabilities=AsyncMock(
                return_value=BrowserCapabilities(auth_gate_visible=True)
            ),
            required_file_upload_pending=AsyncMock(return_value=False),
        )
        middleware = OrchestratorActionOrderMiddleware(browser)
        answer = response("task", {"subagent_type": "AnswerWriter", "description": "Email"})
        authenticate = response(
            "task",
            {"subagent_type": "AuthenticationSpecialist", "description": "Visible login"},
        )
        handler = AsyncMock(side_effect=[answer, authenticate])
        request = SimpleNamespace(messages=[], override=lambda **values: SimpleNamespace(**values))

        self.assertIs(await middleware.awrap_model_call(request, handler), authenticate)


if __name__ == "__main__":
    unittest.main()
