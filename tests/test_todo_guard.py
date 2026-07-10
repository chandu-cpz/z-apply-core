from __future__ import annotations

import unittest
from typing import Any, cast

from langchain_core.messages import AIMessage

from z_apply_core.agents.todo_guard import (
    MAX_TODO_CONTINUATIONS,
    IncompleteTodosError,
    TodoGuardState,
    pending_todo_guard,
)


class PendingTodoGuardTests(unittest.TestCase):
    def test_final_response_with_pending_todos_continues_agent_loop(self) -> None:
        state = cast(
            TodoGuardState,
            {
                "messages": [AIMessage(content="I will stop here.")],
                "todos": [
                    {"content": "Upload resume", "status": "in_progress"},
                    {"content": "Map fields", "status": "pending"},
                ],
            },
        )

        update = pending_todo_guard.after_model(state, cast(Any, None))

        self.assertIsNotNone(update)
        assert update is not None
        self.assertEqual(update["jump_to"], "model")
        self.assertEqual(update["todo_guard_continuations"], 1)
        continuation = update["messages"][0]
        self.assertIn("Upload resume", continuation.content)
        self.assertIn("Map fields", continuation.content)

    def test_completed_todos_allow_final_response(self) -> None:
        state = cast(
            TodoGuardState,
            {
                "messages": [AIMessage(content="Application is ready for review.")],
                "todos": [
                    {"content": "Upload resume", "status": "completed"},
                    {"content": "Map fields", "status": "completed"},
                ],
                "todo_guard_continuations": 2,
            },
        )

        update = pending_todo_guard.after_model(state, cast(Any, None))

        self.assertEqual(update, {"todo_guard_continuations": 0})

    def test_repeated_premature_final_response_fails_as_incomplete(self) -> None:
        state = cast(
            TodoGuardState,
            {
                "messages": [AIMessage(content="Stopping again.")],
                "todos": [{"content": "Upload resume", "status": "pending"}],
                "todo_guard_continuations": MAX_TODO_CONTINUATIONS,
            },
        )

        with self.assertRaisesRegex(IncompleteTodosError, "Upload resume"):
            pending_todo_guard.after_model(state, cast(Any, None))


if __name__ == "__main__":
    unittest.main()
