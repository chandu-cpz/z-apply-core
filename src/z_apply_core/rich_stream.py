from __future__ import annotations

import logging
from typing import Any

from rich.console import Console
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

    @property
    def console(self) -> Console:
        return self._console

    async def accept(self, event: FrameworkTraceEvent) -> None:
        if event.event in {"updates", "values"}:
            self._render_update(event)
            return
        if event.event.startswith("on_chain_"):
            self._render_lifecycle(event, "yellow")
        elif event.event.startswith("on_tool_"):
            self._render_lifecycle(event, "magenta")
        elif event.event.startswith("on_chat_model_"):
            self._render_lifecycle(event, "cyan")

    def print_result(self, result: V3RunResult, state: RunState) -> None:
        self._console.print(
            Panel(
                Text(
                    _clip_text(str(state.get("snapshot", "")) or "No snapshot returned."),
                    overflow="fold",
                ),
                title="Browser Snapshot",
                border_style="green",
            )
        )
        model_id = str(state.get("model_id", ""))
        title = "Orchestrator"
        if model_id:
            title = f"{title} [{model_id}]"
        self._console.print(
            Panel(
                Text(
                    str(state.get("orchestrator_summary", ""))
                    or "No orchestrator summary returned.",
                    overflow="fold",
                ),
                title=Text(title),
                border_style="cyan",
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
            model_suffix = f" [{data['model_id']}]" if data.get("model_id") else ""
            node_info(
                logger,
                "orchestrator",
                "completed%s: %s",
                model_suffix,
                data.get("orchestrator_summary"),
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
                node_info(logger, "orchestrator", "updated DeepAgents working context")
                self._logged_agent_context = True
            return
        if "messages" in keys:
            node_info(logger, "orchestrator", "received model message updates")
            return
        logger.debug("graph state updated: %s", ", ".join(sorted(keys)))

    def _render_lifecycle(self, event: FrameworkTraceEvent, color: str) -> None:
        label = event.event.removeprefix("on_").replace("_", " ")
        preview = _preview(event.data)
        self._console.print(
            Panel(
                Text(preview, overflow="fold"),
                title=f"{label}: {event.name}",
                border_style=color,
            )
        )


def _preview(value: Any, limit: int = 240) -> str:
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _clip_text(text: str, *, max_lines: int = 80, max_chars: int = 6000) -> str:
    lines = text.splitlines()
    clipped = "\n".join(lines[:max_lines])
    omitted_lines = max(0, len(lines) - max_lines)
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 3] + "..."
    if omitted_lines:
        clipped = f"{clipped}\n[... {omitted_lines} more lines omitted from display]"
    return clipped
