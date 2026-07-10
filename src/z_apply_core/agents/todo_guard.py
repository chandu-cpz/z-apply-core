from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from langchain.agents.middleware import AgentState, after_model
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

MAX_TODO_CONTINUATIONS = 3


class TodoItem(TypedDict):
    content: str
    status: Literal["pending", "in_progress", "completed"]


class TodoGuardState(AgentState):
    todos: NotRequired[list[TodoItem]]
    todo_guard_continuations: NotRequired[int]


class IncompleteTodosError(RuntimeError):
    def __init__(self, unfinished: list[str]) -> None:
        self.unfinished = tuple(unfinished)
        super().__init__(
            "Agent repeatedly tried to finish with incomplete todos: " + "; ".join(unfinished)
        )


@after_model(
    state_schema=TodoGuardState,
    can_jump_to=["model"],
    name="PendingTodoGuard",
)
def pending_todo_guard(
    state: TodoGuardState,
    _runtime: Runtime[None],
) -> dict[str, object] | None:
    messages = state["messages"]
    if not messages:
        return None

    last_message = messages[-1]
    if not isinstance(last_message, AIMessage) or last_message.tool_calls:
        return None

    todos = state.get("todos", [])
    unfinished = [todo["content"] for todo in todos if todo.get("status") != "completed"]
    if todos and not unfinished:
        return {"todo_guard_continuations": 0}
    if not todos:
        unfinished = ["Create and maintain the todo list for this multi-step run"]

    continuation_count = state.get("todo_guard_continuations", 0)
    if continuation_count >= MAX_TODO_CONTINUATIONS:
        raise IncompleteTodosError(unfinished)

    pending_lines = "\n".join(f"- {item}" for item in unfinished)
    return {
        "messages": [
            HumanMessage(
                content=(
                    "The run is not complete. Continue working instead of returning a "
                    "final response. Use write_todos to keep the plan current and finish "
                    f"each required item. Unfinished work:\n{pending_lines}"
                )
            )
        ],
        "todo_guard_continuations": continuation_count + 1,
        "jump_to": "model",
    }
