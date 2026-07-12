from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage, HumanMessage

InvocationPattern = re.compile(
    r"(?m)(?:^|(?<=\s))([A-Za-z_][A-Za-z0-9_.-]{0,80})\s*\("
)
ResultMarker = re.compile(r"(?m)^\s*([A-Z][A-Z0-9_]{2,40})_RESULT\s*:")
JSONToolCall = re.compile(
    r"\{[^{}]*\"(?:tool|name)\"\s*:\s*\"([^\"]+)\""
)

_TOOLS_ONLY_NAMES = frozenset({"write_todos", "ask_human", "request_submit_approval", "task"})


@dataclass(frozen=True, slots=True)
class ToolProtocolViolationDetail:
    kind: Literal[
        "exact_prose_tool_call",
        "json_tool_call_imitation",
        "fabricated_transcript",
    ]
    detected_name: str | None
    content_excerpt: str


class ToolProtocolViolation(RuntimeError):
    """A model claimed tool execution without emitting native tool calls."""


class ProseToolCallGuardMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Reject fabricated tool transcripts and enforce native-only tool calling.

    On the first violation, inject a bounded correction message and retry once.
    If the retry also violates, raise ``ToolProtocolViolation``.
    """

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT]:
        if not request.tools:
            result_no_tools: ModelResponse[ResponseT] = await handler(request)
            return result_no_tools

        tool_names = _available_tool_names(request)
        tool_names.update(_TOOLS_ONLY_NAMES)

        result: ModelResponse[ResponseT] = await handler(request)
        violations = _detect_violations(result.result, tool_names)

        if not violations:
            return result

        correction = _correction_message(violations, result.result)
        retry_request = request.override(
            messages=[*request.messages, *correction],
        )
        retry_result: ModelResponse[ResponseT] = await handler(retry_request)
        retry_violations = _detect_violations(retry_result.result, tool_names)

        if retry_violations:
            raise ToolProtocolViolation(
                "tool_protocol_failure: model emitted fabricated tool calls or "
                f"specialist transcripts after correction ({retry_violations[0].kind})"
            )
        return retry_result


def _available_tool_names(request: ModelRequest[Any]) -> set[str]:
    names: set[str] = set()
    for tool in request.tools:
        if hasattr(tool, "name"):
            names.add(str(tool.name))
        elif isinstance(tool, dict):
            name = tool.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text_val = item.get("text", "")
                if isinstance(text_val, str):
                    parts.append(text_val)
        return "\n".join(parts)
    return ""


def _detect_violations(
    messages: list[Any],
    tool_names: set[str],
) -> list[ToolProtocolViolationDetail]:
    violations: list[ToolProtocolViolationDetail] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        text = _message_text(message.content)
        if not text:
            continue
        violations.extend(_check_exact_tool_calls(text, tool_names))
        violations.extend(_check_json_imitations(text, tool_names))
        violations.extend(_check_fabricated_transcripts(text, tool_names))
    return violations


def _check_exact_tool_calls(
    text: str,
    tool_names: set[str],
) -> list[ToolProtocolViolationDetail]:
    violations: list[ToolProtocolViolationDetail] = []
    for match in InvocationPattern.finditer(text):
        candidate = match.group(1)
        if candidate in tool_names:
            excerpt = text[max(0, match.start() - 40) : min(len(text), match.end() + 40)]
            violations.append(
                ToolProtocolViolationDetail(
                    kind="exact_prose_tool_call",
                    detected_name=candidate,
                    content_excerpt=excerpt,
                )
            )
        elif "." in candidate:
            for part in candidate.split("."):
                if part in tool_names:
                    excerpt = text[max(0, match.start() - 40) : min(len(text), match.end() + 40)]
                    violations.append(
                        ToolProtocolViolationDetail(
                            kind="exact_prose_tool_call",
                            detected_name=candidate,
                            content_excerpt=excerpt,
                        )
                    )
                    break
    return violations


def _check_json_imitations(
    text: str,
    tool_names: set[str],
) -> list[ToolProtocolViolationDetail]:
    violations: list[ToolProtocolViolationDetail] = []
    for match in JSONToolCall.finditer(text):
        tool_name = match.group(1)
        if tool_name in tool_names:
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 20)
            excerpt = text[start:end]
            violations.append(
                ToolProtocolViolationDetail(
                    kind="json_tool_call_imitation",
                    detected_name=tool_name,
                    content_excerpt=excerpt,
                )
            )
    return violations


def _check_fabricated_transcripts(
    text: str,
    tool_names: set[str],
) -> list[ToolProtocolViolationDetail]:
    violations: list[ToolProtocolViolationDetail] = []
    for match in ResultMarker.finditer(text):
        prefix = match.group(1)
        if prefix not in tool_names:
            continue
        line_start = text.rfind("\n", 0, match.start())
        preceding = text[max(0, line_start + 1) : match.start()]
        if InvocationPattern.search(preceding) or JSONToolCall.search(preceding):
            excerpt = text[max(0, match.start() - 60) : min(len(text), match.end() + 40)]
            violations.append(
                ToolProtocolViolationDetail(
                    kind="fabricated_transcript",
                    detected_name=f"{prefix}_RESULT",
                    content_excerpt=excerpt,
                )
            )
    return violations


def _correction_message(
    violations: list[ToolProtocolViolationDetail],
    messages: list[Any],
) -> list[HumanMessage]:
    text_parts = [_message_text(m.content) for m in messages if isinstance(m, AIMessage)]
    raw = "\n".join(text_parts)
    excerpt = raw[:600]
    detected = {v.detected_name for v in violations if v.detected_name}
    names_str = ", ".join(sorted(detected)) if detected else "tool invocation syntax"
    return [
        HumanMessage(
            content=(
                f"RUNTIME PROTOCOL ERROR\n\n"
                f"Your previous response attempted to represent native tool execution "
                f"as assistant text ({names_str}). Nothing in that text executed.\n\n"
                f"Retry the same intended action now using the actual native "
                f"tool-calling interface.\n\n"
                f"Do not print tool_name(...).\n"
                f"Do not print JSON pretending to be a tool call.\n"
                f"Do not invent specialist or verifier results.\n"
                f"Do not explain the correction.\n"
                f"Perform the intended native tool call directly.\n\n"
                f"Your invalid output (truncated):\n{excerpt}"
            ),
        ),
    ]
