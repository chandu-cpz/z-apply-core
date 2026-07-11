from __future__ import annotations

import logging
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from z_apply_core.log_labels import agent_info, node_info, run_info
from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkTraceEvent, V3RunResult

logger = logging.getLogger(__name__)


class RichStreamRenderer:
    """Render framework stream events as chronological terminal output.

    Token deltas are buffered until a stream boundary (tool event, lifecycle
    event, completed message, or run close), then printed as static panels so
    the terminal stays in chronological order without a Live overlay.
    """

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._logged_run_start = False
        self._logged_snapshot = False
        self._logged_agent_context = False
        self._stream_active = False
        self._reasoning_text = ""
        self._content_text = ""
        self._stream_source = "model"

    @property
    def console(self) -> Console:
        return self._console

    def close(self) -> None:
        self._end_stream_if_active()

    async def accept(self, event: FrameworkTraceEvent) -> None:
        if event.event in {"updates", "values", "auth"}:
            self._render_update(event)
            return

        if event.event == "messages":
            # DeepAgents messages are already emitted through the custom
            # ``agent_*`` events below. Rendering this generic projection as
            # well duplicates partial and completed model output.
            return

        if event.event == "agent_model_tool_call":
            return

        if event.event in {
            "agent_message",
            "agent_message_delta",
            "agent_tool_start",
            "agent_tool_delta",
            "agent_tool_end",
            "agent_lifecycle",
        }:
            self._render_agent_event(event)
            return

        if event.event in ("on_tool_start", "on_tool_end"):
            self._end_stream_if_active()
            self._render_tool_event(event)
            return

        if event.event == "lifecycle":
            self._end_stream_if_active()
            self._render_deepagents_lifecycle(event)
            return

        if event.event.startswith("on_"):
            # Ignore standard LangChain events to avoid duplication with agent
            # stream projections.
            return

        self._end_stream_if_active()

    def print_result(self, result: V3RunResult, state: RunState) -> None:
        self._end_stream_if_active()
        model_id = str(state.get("model_id", ""))
        status = str(state.get("run_status", "")) or "unknown"
        title = f"Run result: {status}"
        if model_id:
            title = f"{title} [{model_id}]"
        border_style = {
            "completed": "green",
            "incomplete": "yellow",
            "failed": "red",
        }.get(status, "red")
        self._console.print(
            Panel(
                Markdown(
                    str(state.get("orchestrator_summary", ""))
                    or "No orchestrator summary returned."
                ),
                title=Text(title),
                border_style="red" if result.errors else border_style,
            )
        )
        run_info(logger, "streamed %s events in %sms", result.event_count, result.duration_ms)

    def _render_update(self, event: FrameworkTraceEvent) -> None:
        data = event.data.get("data", event.data)
        if isinstance(data, dict) and data.get("snapshot"):
            if not self._logged_snapshot:
                node_info(logger, "setup_browser", "opened page and captured snapshot")
                self._logged_snapshot = True
            return
        if isinstance(data, dict) and data.get("orchestrator_summary"):
            # ``print_result`` renders the authoritative final summary once the
            # stream has closed. Rendering this update as well duplicates it.
            return
        if isinstance(data, dict) and data.get("auth_summary"):
            # Authentication emits a dedicated typed event before this graph
            # state update, so the update is intentionally silent.
            return
        if event.event == "auth":
            node_info(
                logger,
                "authenticate_default_account",
                "%s: %s",
                event.data.get("status") or "unknown",
                event.data.get("summary") or "",
            )
            return
        if isinstance(data, dict) and data.get("job_url"):
            if not self._logged_run_start:
                run_info(logger, "starting %s", data["job_url"])
                self._logged_run_start = True
            return
        if isinstance(data, dict):
            self._render_state_update(data)
            return
        logger.debug("graph update %s", event.name)

    def _render_state_update(self, data: dict[str, object]) -> None:
        keys = set(data)
        if {"messages", "files"}.issubset(keys):
            if not self._logged_agent_context:
                self._end_stream_if_active()
                node_info(logger, "orchestrator", "updated DeepAgents working context")
                self._logged_agent_context = True
            return
        if "messages" in keys:
            self._end_stream_if_active()
            return
        logger.debug("graph state updated: %s", ", ".join(sorted(keys)))

    def _render_agent_event(self, event: FrameworkTraceEvent) -> None:
        if event.event == "agent_message_delta":
            if self._stream_active and event.name != self._stream_source:
                self._end_stream_if_active()
            if not self._stream_active:
                self._start_stream(event.name)
            kind = str(event.data.get("kind", "text"))
            delta = _dedupe_delta(
                self._reasoning_text if kind == "reasoning" else self._content_text,
                str(event.data.get("delta", "")),
            )
            if kind == "reasoning":
                self._reasoning_text += delta
            else:
                self._content_text += delta
            return

        if event.event == "agent_message":
            self._end_stream_if_active(persist_content=False)
            source = event.name
            reasoning = str(event.data.get("reasoning") or "")
            text = str(event.data.get("text") or "")
            if reasoning:
                self._console.print(
                    Panel(
                        Text(reasoning, style="dim gray50", overflow="fold"),
                        title=Text(f"{source} thinking"),
                        border_style="dim gray50",
                    )
                )
            if text:
                self._console.print(
                    Panel(
                        Markdown(text),
                        title=Text(f"{source} response"),
                        border_style="cyan",
                    )
                )
            return

        if event.event == "agent_tool_start":
            self._end_stream_if_active()
            tool_name = str(event.data.get("tool_name", "tool"))
            self._console.print(
                Panel(
                    Text(_preview(event.data.get("input", ""), limit=600), overflow="fold"),
                    title=Text(f"{event.name} tool start: {tool_name}"),
                    border_style="magenta",
                )
            )
            return

        if event.event == "agent_tool_delta":
            # The completed tool event carries the authoritative output.
            # Rendering every partial delta duplicates it.
            return

        if event.event == "agent_tool_end":
            self._end_stream_if_active()
            tool_name = str(event.data.get("tool_name", "tool"))
            error = str(event.data.get("error", ""))
            output = error or _preview(event.data.get("output", ""), limit=500)
            self._console.print(
                Panel(
                    Text(output, overflow="fold"),
                    title=Text(f"{event.name} tool end: {tool_name}"),
                    border_style="red" if error else "green",
                )
            )
            return

        if event.event == "agent_lifecycle":
            self._end_stream_if_active()
            status = str(event.data.get("status", ""))
            detail = str(event.data.get("error") or event.data.get("path") or "")
            suffix = f": {detail}" if detail else ""
            if status == "failed":
                logger.error(
                    "[black on red] AGENT [/][black on bright_blue] %s [/] %s%s",
                    event.name,
                    status,
                    suffix,
                    extra={"markup": True},
                )
            else:
                agent_info(logger, event.name, "%s%s", status, suffix)

    def _start_stream(self, model_name: str) -> None:
        self._end_stream_if_active()
        self._reasoning_text = ""
        self._content_text = ""
        self._stream_source = model_name or "model"
        self._stream_active = True

    def _render_deepagents_lifecycle(self, event: FrameworkTraceEvent) -> None:
        if event.event == "lifecycle":
            data = event.data.get("data")
            if not isinstance(data, dict):
                return

            action = data.get("event")
            graph_name = data.get("graph_name", "Subagent")
            agent_info(logger, str(graph_name), "%s", action)

    def _render_tool_event(self, event: FrameworkTraceEvent) -> None:
        if event.event == "on_tool_start":
            tool_name = event.name
            args = event.data.get("input", "")
            title = f"Tool: {tool_name}"
            body = (
                f"Calling [bold]{escape(str(tool_name))}[/bold] with arguments:\n"
                f"{escape(str(args))}"
            )
            self._console.print(Panel(body, title=title, border_style="magenta"))

        elif event.event == "on_tool_end":
            tool_name = event.name
            result = event.data.get("output", "")
            title = f"Tool Result: {tool_name}"
            body = _preview(result, limit=400)
            self._console.print(Panel(escape(body), title=title, border_style="green"))

    def _end_stream_if_active(self, *, persist_content: bool = True) -> None:
        if not self._stream_active:
            return

        source = self._stream_source
        reasoning = self._reasoning_text.strip()
        content = self._content_text.strip()
        self._stream_active = False
        self._reasoning_text = ""
        self._content_text = ""

        if reasoning:
            self._console.print(
                Panel(
                    Text(reasoning, style="dim gray50", overflow="fold"),
                    title=Text(f"{source} thinking"),
                    border_style="dim gray50",
                )
            )
        if persist_content and content:
            self._console.print(
                Panel(
                    Markdown(content),
                    title=Text(f"{source} response"),
                    border_style="cyan",
                )
            )


def _preview(value: Any, limit: int = 240) -> str:
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _dedupe_delta(existing: str, delta: str) -> str:
    if not delta:
        return ""
    if existing.endswith(delta) and (len(delta) > 1 or not delta.isalnum()):
        return ""
    return delta
