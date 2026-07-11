from __future__ import annotations

import unittest
from inspect import Parameter
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from nim_router import NimRouter

from z_apply_core.agents.browser_action_verification import (
    BrowserActionVerificationMiddleware,
)
from z_apply_core.browser_tools import BrowserToolRegistry
from z_apply_core.stream_events import V3RunResult


def tool_spec(name: str, *parameters: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        title=name,
        description=name,
        parameters=[
            SimpleNamespace(
                name=parameter,
                annotation=str,
                default=Parameter.empty,
                description=parameter,
                hidden=False,
            )
            for parameter in parameters
        ],
    )


class BrowserActionVerificationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_goal_drives_verification_without_reusing_action_ref(
        self,
    ) -> None:
        backend_calls: list[tuple[str, dict[str, Any]]] = []

        async def backend(name: str, arguments: dict[str, Any]) -> str:
            backend_calls.append((name, arguments))
            if name == "browser_snapshot":
                return '- heading "Apply for this job"\n- button "Choose File"'
            return "click completed"

        registry = BrowserToolRegistry(
            [tool_spec("browser_click", "target"), tool_spec("browser_snapshot")],
            backend,
        )
        tools = {tool.name: tool for tool in registry.langchain_tools()}
        verifier = MagicMock()
        verifier.astream_events.return_value = object()
        with patch(
            "z_apply_core.agents.browser_action_verification.create_deep_agent",
            return_value=verifier,
        ):
            middleware = BrowserActionVerificationMiddleware(
                fallback_model=MagicMock(),
                router=NimRouter(),
                read_only_browser_tools=[tools["browser_snapshot"]],
                prompt_name="verifier.md",
                verifier_role="Verifier",
            )

        goal = (
            "Open the application form. Success condition: the form and primary "
            "resume control are visible."
        )
        tool_call = {
            "name": "browser_click",
            "args": {"target": "e112", "verification_goal": goal},
            "id": "call-1",
            "type": "tool_call",
        }
        request = ToolCallRequest(
            tool_call=tool_call,
            tool=tools["browser_click"],
            state={},
            runtime=cast(Any, SimpleNamespace()),
        )

        async def execute_tool(call_request: ToolCallRequest) -> ToolMessage:
            tool = cast(BaseTool, call_request.tool)
            return cast(ToolMessage, await tool.ainvoke(call_request.tool_call))

        with patch(
            "z_apply_core.agents.browser_action_verification.consume_deepagent_stream",
            AsyncMock(
                return_value=V3RunResult(
                    output={"messages": [AIMessage(content="verified: form is visible")]}
                )
            ),
        ):
            result = await middleware.awrap_tool_call(request, execute_tool)

        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(
            backend_calls,
            [
                (
                    "browser_click",
                    {
                        "target": "e112",
                        "verification_goal": goal,
                    },
                ),
                ("browser_snapshot", {}),
            ],
        )
        self.assertIn(f"VERIFICATION_GOAL: {goal}", str(result.content))
        self.assertIn("AUTOMATIC_VERIFIER_RESULT: verified", str(result.content))
        verifier_request = verifier.astream_events.call_args.args[0]
        verifier_prompt = verifier_request["messages"][0]["content"]
        self.assertIn("Semantic verification goal: Open the application form", verifier_prompt)
        self.assertIn("may identify different elements now", verifier_prompt)
        self.assertIn("Do not look up or reinterpret those old refs", verifier_prompt)


if __name__ == "__main__":
    unittest.main()
