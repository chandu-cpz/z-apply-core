from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from z_apply_core.stream_events import FrameworkTraceEvent, V3RunResult


class RichStreamRenderer:
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

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

    def print_result(self, result: V3RunResult, snapshot: str) -> None:
        self._console.print(
            Panel(
                Text(snapshot or "No snapshot returned.", overflow="fold"),
                title="Browser Snapshot",
                border_style="green",
            )
        )
        self._console.print(
            f"[dim]streamed {result.event_count} events in {result.duration_ms}ms[/dim]"
        )

    def _render_update(self, event: FrameworkTraceEvent) -> None:
        data = event.data.get("data", event.data)
        if getattr(data, "snapshot", ""):
            self._console.print("[green]setup_browser[/green] opened page and captured snapshot")
            return
        if getattr(data, "job_url", ""):
            self._console.print(f"[cyan]run[/cyan] starting {data.job_url}")
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
