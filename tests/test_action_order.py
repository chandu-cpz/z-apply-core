from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from z_apply_core.agents.action_order import OrchestratorActionOrderMiddleware
from z_apply_core.browser_observation import BrowserCapabilities


def response(name: str, args: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        result=[AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])]
    )


class ActionOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_writer_result_must_be_consumed_by_browser_mutation(self) -> None:
        browser = SimpleNamespace(required_file_upload_pending=AsyncMock(return_value=False))
        middleware = OrchestratorActionOrderMiddleware(browser)
        first = response("task", {"subagent_type": "AnswerWriter", "description": "Name"})
        repeat = response("task", {"subagent_type": "AnswerWriter", "description": "Name"})
        fill = response("browser_fill_form", {"fields": []})
        handler = AsyncMock(side_effect=[first, repeat, fill])
        request = SimpleNamespace(messages=[], override=lambda **values: SimpleNamespace(**values))

        self.assertIs(await middleware.awrap_model_call(request, handler), first)
        self.assertIs(await middleware.awrap_model_call(request, handler), fill)
        self.assertEqual(handler.await_count, 3)

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
