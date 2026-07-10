from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from nim_router import NimRouter

from z_apply_core.agents.browser_action_verification import (
    BrowserActionVerificationMiddleware,
)


class BrowserActionVerificationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_mutating_browser_action_is_automatically_verified(self) -> None:
        with patch(
            "z_apply_core.agents.browser_action_verification.create_deep_agent",
            return_value=MagicMock(),
        ):
            middleware = BrowserActionVerificationMiddleware(
                fallback_model=MagicMock(),
                router=NimRouter(),
                read_only_browser_tools=[],
                prompt_name="verifier.md",
                verifier_role="Verifier",
            )
        middleware._verify = AsyncMock(return_value="verified: application form is visible.")  # type: ignore[method-assign]

        request = ToolCallRequest(
            tool_call={
                "name": "browser_click",
                "args": {"target": "e1"},
                "id": "call-1",
                "type": "tool_call",
            },
            tool=None,
            state={},
            runtime=cast(Any, SimpleNamespace()),
        )

        async def handler(_request: ToolCallRequest) -> ToolMessage:
            return ToolMessage(content="action completed", tool_call_id="call-1")

        result = await middleware.awrap_tool_call(request, handler)

        self.assertIsInstance(result, ToolMessage)
        self.assertIn("AUTOMATIC_VERIFIER_RESULT: verified", str(result.content))


if __name__ == "__main__":
    unittest.main()
