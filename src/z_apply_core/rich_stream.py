from __future__ import annotations

import logging
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from z_apply_core.log_labels import node_info, run_info
from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkTraceEvent, V3RunResult

logger = logging.getLogger(__name__)


class RichStreamRenderer:
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._logged_run_start = False
        self._logged_snapshot = False
        self._logged_agent_context = False
        self._live: Live | None = None
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
            self._render_messages(event)
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
            # Ignore standard LangChain events to avoid duplication with 'messages' stream
            return

        self._end_stream_if_active()

    def print_result(self, result: V3RunResult, state: RunState) -> None:
        self._end_stream_if_active()
        model_id = str(state.get("model_id", ""))
        title = "Run result"
        if model_id:
            title = f"{title} [{model_id}]"
        self._console.print(
            Panel(
                Markdown(
                    str(state.get("orchestrator_summary", ""))
                    or "No orchestrator summary returned."
                ),
                title=Text(title),
                border_style="green" if not result.errors else "red",
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
            if self._live and event.name != self._stream_source:
                self._end_stream_if_active()
            if not self._live:
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
            self._refresh_live_message()
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
            # The completed tool event carries the authoritative output. Rendering
            # every partial delta duplicates it and disrupts Live's chronological
            # placement at the bottom of the terminal.
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
            style = {
                "failed": "bold red",
                "completed": "green",
            }.get(status, "yellow")
            line = Text.assemble(
                ("Agent ", "dim"),
                (event.name, "bold"),
                (f" {status}", style),
            )
            if detail:
                line.append(f": {detail}")
            self._console.print(line)

    def _start_stream(self, model_name: str) -> None:
        self._end_stream_if_active()
        self._reasoning_text = ""
        self._content_text = ""
        self._stream_source = model_name or "model"
        self._live = Live(
            Group(),
            console=self._console,
            auto_refresh=False,
            transient=True,
            vertical_overflow="ellipsis",
        )
        self._live.start()

    def _render_messages(self, event: FrameworkTraceEvent) -> None:
        data_tuple = event.data.get("data")
        if not (isinstance(data_tuple, (list, tuple)) and len(data_tuple) > 0):
            return

        chunk = data_tuple[0]
        if not isinstance(chunk, dict):
            self._render_message_chunk_object(chunk, data_tuple)
            return

        chunk_event = chunk.get("event")

        if chunk_event == "message-start":
            model_name = "model"
            if len(data_tuple) > 1 and isinstance(data_tuple[1], dict):
                model_name = data_tuple[1].get("ls_model_name", "model")
            self._start_stream(model_name)

        elif chunk_event == "content-block-delta":
            delta = chunk.get("delta")
            if isinstance(delta, dict):
                if delta.get("type") == "reasoning-delta":
                    text_delta = _dedupe_delta(
                        self._reasoning_text,
                        str(delta.get("reasoning", "")),
                    )
                    self._reasoning_text += text_delta
                elif delta.get("type") == "text-delta":
                    text_delta = _dedupe_delta(
                        self._content_text,
                        str(delta.get("text", "")),
                    )
                    self._content_text += text_delta
            self._refresh_live_message()

        elif chunk_event == "tool-started":
            self._end_stream_if_active()
            tool_name = chunk.get("tool_call", {}).get("name", "unknown")
            args = chunk.get("tool_call", {}).get("arguments", "")
            title = f"Tool: {tool_name}"
            body = (
                f"Calling [bold]{escape(str(tool_name))}[/bold] with arguments:\n"
                f"{escape(str(args))}"
            )
            self._console.print(Panel(body, title=title, border_style="magenta"))

        elif chunk_event == "tool-finished":
            self._end_stream_if_active()
            result = chunk.get("result")
            tool_name = getattr(result, "name", "unknown")
            title = f"Tool Result: {tool_name}"

            content = getattr(result, "content", str(result)) if result else chunk.get("output", "")
            body = _preview(content, limit=400)
            self._console.print(Panel(escape(body), title=title, border_style="green"))

        elif chunk_event == "message-finish":
            self._end_stream_if_active()

    def _render_message_chunk_object(
        self,
        chunk: object,
        data_tuple: list[Any] | tuple[Any, ...],
    ) -> None:
        metadata = data_tuple[1] if len(data_tuple) > 1 and isinstance(data_tuple[1], dict) else {}
        model_name = str(metadata.get("ls_model_name") or metadata.get("langgraph_node") or "model")
        if not self._live:
            self._start_stream(model_name)

        text = getattr(chunk, "text", "")
        if isinstance(text, str) and text:
            self._content_text += _dedupe_delta(self._content_text, text)

        content = getattr(chunk, "content", None)
        if isinstance(content, str) and content and content != text:
            self._content_text += _dedupe_delta(self._content_text, content)

        for block in _list_attr(chunk, "content_blocks"):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"text", "text_delta"}:
                self._content_text += _dedupe_delta(
                    self._content_text,
                    str(block.get("text") or ""),
                )
            elif block_type in {"reasoning", "reasoning_delta"}:
                self._reasoning_text += _dedupe_delta(
                    self._reasoning_text,
                    str(block.get("reasoning") or block.get("text") or block.get("content") or ""),
                )

        self._refresh_live_message()

    def _refresh_live_message(self) -> None:
        if not self._live:
            return
        renderables: list[Any] = []
        if self._reasoning_text:
            renderables.append(
                Panel(
                    Text(self._reasoning_text.strip(), style="dim gray50"),
                    title=Text(f"{self._stream_source} thinking"),
                    border_style="dim gray50",
                )
            )
            if self._content_text:
                renderables.append(Text(""))

        if self._content_text:
            renderables.append(Markdown(self._content_text.strip()))

        if renderables:
            self._live.update(Group(*renderables), refresh=True)

    def _render_deepagents_lifecycle(self, event: FrameworkTraceEvent) -> None:
        if event.event == "lifecycle":
            data = event.data.get("data")
            if not isinstance(data, dict):
                return

            action = data.get("event")
            graph_name = data.get("graph_name", "Subagent")
            style = "green" if action == "completed" else "yellow"
            self._console.print(
                Text.assemble(
                    ("Agent ", "dim"),
                    (str(graph_name), "bold"),
                    (f" {action}", style),
                )
            )

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
        if not self._live:
            return

        live = self._live
        source = self._stream_source
        reasoning = self._reasoning_text.strip()
        content = self._content_text.strip()
        self._live = None
        self._reasoning_text = ""
        self._content_text = ""
        live.stop()

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

    def _render_lifecycle(self, event: FrameworkTraceEvent, color: str) -> None:
        label = event.event.removeprefix("on_").replace("_", " ")
        preview = _preview(event.data)
        self._console.print(
            Panel(
                Text(preview, overflow="fold"),
                title=escape(f"{label}: {event.name}"),
                border_style=color,
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


def _list_attr(value: object, attr: str) -> list[Any]:
    items = getattr(value, attr, None)
    if isinstance(items, list):
        return items
    return []
