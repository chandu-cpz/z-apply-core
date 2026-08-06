from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage, HumanMessage

RuntimeEvidenceMarker = re.compile(
    r"(?m)^\s*(BROWSER ACTION RECEIPT|BROWSER OBSERVATION)\b"
)


@dataclass(frozen=True, slots=True)
class ToolProtocolViolationDetail:
    kind: Literal["fabricated_runtime_evidence"]
    detected_name: str | None
    content_excerpt: str


class ToolProtocolViolation(RuntimeError):
    """A model claimed runtime-owned evidence without the runtime producing it."""


class ProseToolCallGuardMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Reject fabricated runtime evidence and enforce native-only tool calling.

    Only typed boundaries count as violations: ``BROWSER ACTION RECEIPT`` and
    ``BROWSER OBSERVATION`` markers are emitted exclusively by the runtime, so
    a model writing them is forging the evidence channel. On the first
    violation, inject a bounded correction message and retry once. If the
    retry also violates, raise ``ToolProtocolViolation``.
    """

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT]:
        result: ModelResponse[ResponseT] = await handler(request)
        violations = _detect_violations(result.result)

        if not violations:
            return result

        correction = _correction_message(violations)
        retry_request = request.override(
            messages=[*request.messages, *correction],
        )
        retry_result: ModelResponse[ResponseT] = await handler(retry_request)
        retry_violations = _detect_violations(retry_result.result)

        if retry_violations:
            raise ToolProtocolViolation(
                "tool_protocol_failure: model fabricated runtime-owned "
                f"browser evidence after correction ({retry_violations[0].kind})"
            )
        return retry_result


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


def _detect_violations(messages: list[Any]) -> list[ToolProtocolViolationDetail]:
    violations: list[ToolProtocolViolationDetail] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        text = _message_text(message.content)
        if not text:
            continue
        violations.extend(_check_fabricated_runtime_evidence(text))
    return violations


def _check_fabricated_runtime_evidence(
    text: str,
) -> list[ToolProtocolViolationDetail]:
    """Reject browser evidence markers that only the runtime may produce."""
    violations: list[ToolProtocolViolationDetail] = []
    for match in RuntimeEvidenceMarker.finditer(text):
        violations.append(
            ToolProtocolViolationDetail(
                kind="fabricated_runtime_evidence",
                detected_name=match.group(1),
                content_excerpt=text[
                    max(0, match.start() - 40) : min(len(text), match.end() + 80)
                ],
            )
        )
    return violations


def _correction_message(
    violations: list[ToolProtocolViolationDetail],
) -> list[HumanMessage]:
    detected = {v.detected_name for v in violations if v.detected_name}
    names_str = ", ".join(sorted(detected)) if detected else "browser evidence"
    return [
        HumanMessage(
            content=(
                "RUNTIME PROTOCOL ERROR: your previous response wrote "
                f"{names_str} inside assistant content. Those markers are "
                "emitted only by the runtime; never write or predict them "
                "yourself. Discard that response. Retry the same action now "
                "through the native tool-call channel with no accompanying "
                "assistant content."
            ),
        ),
    ]
