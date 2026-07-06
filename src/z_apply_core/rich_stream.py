from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkTraceEvent, V3RunResult


class RichStreamRenderer:
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

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
        status = str(state.get("status", ""))
        model_id = str(state.get("model_id", ""))
        title = f"Orchestrator: {status or 'unknown'}"
        if model_id:
            title = f"{title} [{model_id}]"
        self._console.print(
            Panel(
                Text(
                    str(state.get("reason", "")) or "No orchestrator reason returned.",
                    overflow="fold",
                ),
                title=Text(title),
                border_style="cyan" if status == "success" else "yellow",
            )
        )
        self._console.print(
            f"[dim]streamed {result.event_count} events in {result.duration_ms}ms[/dim]"
        )

    def _render_update(self, event: FrameworkTraceEvent) -> None:
        data = event.data.get("data", event.data)
        if isinstance(data, dict) and data.get("snapshot"):
            self._console.print("[green]setup_browser[/green] opened page and captured snapshot")
            return
        if isinstance(data, dict) and data.get("status"):
            model_suffix = f" [{data['model_id']}]" if data.get("model_id") else ""
            message = Text("orchestrator ", style="cyan")
            message.append(f"{data.get('status')}{model_suffix}: {data.get('reason')}")
            self._console.print(message)
            return
        if isinstance(data, dict) and data.get("job_url"):
            self._console.print(f"[cyan]run[/cyan] starting {data['job_url']}")
            return
        names = ", ".join(str(name) for name in data) if isinstance(data, dict) else event.name
        self._console.print(f"[green]graph update[/green] {names}")

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
