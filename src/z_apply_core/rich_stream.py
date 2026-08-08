from __future__ import annotations

import logging
import time
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.context.cost_estimate import format_cost
from z_apply_core.context.token_metric import TokenUsage
from z_apply_core.log_labels import agent_info, node_info, run_info
from z_apply_core.state import RunState
from z_apply_core.stream_events import (
    FrameworkTraceEvent,
    TokenUsageEvent,
    V3RunResult,
)

logger = logging.getLogger(__name__)


def _humanize_cache(tokens: int) -> str:
    """Render cache-read tokens compactly (e.g. ``12.0k``), ``-`` when none."""
    if tokens <= 0:
        return "-"
    if tokens >= 1000:
        return f"{tokens / 1000.0:.1f}k"
    return str(tokens)


# TokenUsageEvent is rendered at most once per interval, and immediately when
# the measured values change, so a model loop cannot spam the terminal.
_TOKEN_USAGE_THROTTLE_SECONDS = 10.0


class RichStreamRenderer:
    """Render framework stream events as chronological terminal output.

    Model content (reasoning, responses, tool calls) arrives from the
    middleware ``model_call_start`` / ``model_call_content`` events, which fire
    for every LLM call in every phase; the graph's experimental v3 stream
    projections are only used for tool and lifecycle panels.
    """

    _LEDGER_MAX_ROWS = 25
    _LEDGER_CHECKPOINT_INTERVAL = 10

    def __init__(
        self,
        console: Console | None = None,
        ledger: RunCallLedger | None = None,
    ) -> None:
        self._console = console or Console()
        self._ledger = ledger
        self._logged_run_start = False
        self._logged_snapshot = False
        self._logged_agent_context = False
        self._last_token_usage: TokenUsage | None = None
        self._last_token_usage_time = 0.0
        self._last_ttft_ms: int | None = None

    @property
    def console(self) -> Console:
        return self._console

    async def accept(self, event: FrameworkTraceEvent | TokenUsageEvent) -> None:
        if isinstance(event, TokenUsageEvent):
            self._render_token_usage(event)
            return

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
        }:
            # Model content is sourced from the middleware ``model_call_*``
            # events, which fire for every LLM call in every phase. The
            # projection-sourced messages are unreliable and would duplicate
            # the same thinking/response panels.
            return

        if event.event in {
            "agent_turn",
            "agent_tool_start",
            "agent_tool_delta",
            "agent_tool_end",
            "agent_lifecycle",
        }:
            self._render_agent_event(event)
            return

        if event.event in ("on_tool_start", "on_tool_end"):
            self._render_tool_event(event)
            return

        if event.event == "lifecycle":
            self._render_deepagents_lifecycle(event)
            return

        if event.event == "model_call_rejected":
            self._render_rejection(event)
            return

        if event.event == "model_failed":
            self._render_model_failed(event)
            return

        if event.event == "model_call_start":
            self._render_model_call_start(event)
            return

        if event.event == "model_call_content":
            self._render_model_call_content(event)
            return

        if event.event == "model_call_metrics":
            # The content event carries the same metrics plus the model's
            # reasoning/text/tool calls, so this bare line stays silent; the
            # event is the per-successful-call trigger for the run ledger.
            self._render_call_ledger()
            return

        if event.event in {"recovery_started", "recovery_completed", "recovery_exhausted"}:
            self._render_recovery(event)
            return

        if event.event == "active_objective_rejected":
            self._render_objective_rejected(event)
            return

        if event.event.startswith("on_"):
            # Ignore standard LangChain events to avoid duplication with agent
            # stream projections.
            return

    def print_result(self, result: V3RunResult, state: RunState) -> None:
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
        if self._last_ttft_ms is not None:
            run_info(logger, "last agent turn ttft: %sms", self._last_ttft_ms)

    def _render_token_usage(self, event: TokenUsageEvent) -> None:
        now = time.monotonic()
        usage = event.usage
        unchanged = usage == self._last_token_usage
        if (
            unchanged
            and self._last_token_usage_time > 0
            and now - self._last_token_usage_time < _TOKEN_USAGE_THROTTLE_SECONDS
        ):
            return
        self._last_token_usage = usage
        self._last_token_usage_time = now
        self._console.print(Text(f"token usage: {usage}", style="dim"))

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
                node_info(logger, "orchestrator", "updated DeepAgents working context")
                self._logged_agent_context = True
            return
        if "messages" in keys:
            return
        logger.debug("graph state updated: %s", ", ".join(sorted(keys)))

    def _render_agent_event(self, event: FrameworkTraceEvent) -> None:
        # ``agent_message`` / ``agent_message_delta`` are intentionally not
        # rendered here: model content arrives via the middleware
        # ``model_call_content`` event, which is reliable in every phase.
        if event.event == "agent_turn":
            agent = event.name
            data = event.data
            parts: list[str] = []
            ttft_ms = data.get("ttft_ms")
            if isinstance(ttft_ms, int) and ttft_ms <= 0:
                # The consumer-side first-delta time is 0ms by construction
                # (first delta arrives in the same burst as the part). Prefer
                # the model-side first-chunk measurement when it exists.
                ttft_ms = self._last_ttft_ms
            if isinstance(ttft_ms, int) and ttft_ms > 0:
                self._last_ttft_ms = ttft_ms
                parts.append(f"ttft {ttft_ms}ms")
            duration_ms = data.get("duration_ms")
            if isinstance(duration_ms, int):
                parts.append(f"turn {duration_ms}ms")
            tool_count = len(data.get("tool_calls") or [])
            if tool_count:
                parts.append(f"{tool_count} tool calls")
            suffix = f" · {' · '.join(parts)}" if parts else ""
            agent_info(logger, agent, "turn complete%s", suffix)
            return

        if event.event == "agent_tool_start":
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

    def _render_rejection(self, event: FrameworkTraceEvent) -> None:
        data = event.data
        reason = str(data.get("reason", "model call rejected"))
        middleware = str(data.get("middleware", ""))
        title = f"{event.name} model call rejected"
        if middleware:
            title = f"{title} · {middleware}"
        self._console.print(
            Panel(
                Text(reason, overflow="fold"),
                title=Text(title),
                border_style="yellow",
            )
        )

    def _render_model_failed(self, event: FrameworkTraceEvent) -> None:
        data = event.data
        role = str(data.get("role") or event.name or "agent")
        model_id = str(data.get("model_id", ""))
        error_type = str(data.get("error_type", "error"))
        error = str(data.get("error", "") or error_type)
        title = f"{role} model call failed"
        if model_id:
            title = f"{title} [{model_id}]"
        self._console.print(
            Panel(
                Text(f"{error_type}: {error}", overflow="fold"),
                title=Text(title),
                border_style="red",
            )
        )

    def _render_model_call_start(self, event: FrameworkTraceEvent) -> None:
        data = event.data
        role = str(data.get("role") or event.name or "agent")
        model_id = str(data.get("model_id", ""))
        provider = str(data.get("provider", "?"))
        parts: list[str] = []
        estimate = data.get("input_tokens_estimate")
        if isinstance(estimate, int):
            parts.append(f"in≈{estimate} tok")
        tool_count = data.get("tool_count")
        if isinstance(tool_count, int):
            parts.append(f"{tool_count} tools")
        suffix = f" · {' · '.join(parts)}" if parts else ""
        preview = str(data.get("prompt_preview") or "")
        self._console.print(
            Panel(
                Text(preview or "(no prompt preview)", style="dim", overflow="fold"),
                title=Text(f"{role} · {model_id} [{provider}] call{suffix}"),
                border_style="cyan",
            )
        )

    def _render_model_call_content(self, event: FrameworkTraceEvent) -> None:
        data = event.data
        role = str(data.get("role") or event.name or "agent")
        model_id = str(data.get("model_id", ""))
        provider = str(data.get("provider", "?"))
        ttft_ms = data.get("ttft_ms")
        if isinstance(ttft_ms, int) and ttft_ms > 0:
            self._last_ttft_ms = ttft_ms
        reasoning = str(data.get("reasoning") or "")
        text = str(data.get("text") or "")
        if reasoning:
            self._console.print(
                Panel(
                    Text(reasoning, style="dim gray50", overflow="fold"),
                    title=Text(f"{role} thinking"),
                    border_style="dim gray50",
                )
            )
        if text:
            self._console.print(
                Panel(
                    Markdown(text),
                    title=Text(f"{role} response"),
                    border_style="cyan",
                )
            )
        tool_calls = data.get("tool_calls") or []
        call_lines = [_render_tool_call_line(call) for call in tool_calls if isinstance(call, dict)]
        if call_lines:
            self._console.print(
                Panel(
                    Text("\n".join(call_lines), overflow="fold"),
                    title=Text(f"{role} tool calls"),
                    border_style="magenta",
                )
            )
        parts: list[str] = []
        if isinstance(ttft_ms, int):
            parts.append(f"ttft {ttft_ms}ms")
        duration_ms = data.get("duration_ms")
        if isinstance(duration_ms, int) and duration_ms > 0:
            parts.append(f"{duration_ms / 1000:.2f}s")
        input_tokens = data.get("input_tokens")
        output_tokens = data.get("output_tokens")
        cache_read = data.get("cache_read_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            parts.append(f"in={input_tokens} out={output_tokens}")
        if isinstance(cache_read, int):
            if isinstance(input_tokens, int) and input_tokens > 0:
                parts.append(f"hit={cache_read} ({cache_read * 100 // input_tokens}%)")
            else:
                parts.append(f"cache={cache_read}")
        suffix = f" · {' · '.join(parts)}" if parts else ""
        logger.info("model %s [%s] %s call complete%s", model_id, provider, role, suffix)

    def _render_call_ledger(self) -> None:
        """Show the newest call as one line; print the full table at checkpoints.

        A full-table print per call would flood the terminal on long runs, so
        each successful call renders a compact increment line with the running
        total, and the cumulative table prints every
        ``_LEDGER_CHECKPOINT_INTERVAL`` calls plus once at run end.
        """
        if self._ledger is None or not self._ledger.entries:
            return
        if self._ledger.call_count % self._LEDGER_CHECKPOINT_INTERVAL == 0:
            self._console.print(self._ledger_table())
            return
        self._console.print(self._ledger_increment_line())

    def _ledger_increment_line(self) -> Text:
        assert self._ledger is not None
        entry = self._ledger.entries[-1]
        return Text(
            f"LLM {entry.sequence}/{self._ledger.call_count} \u00b7 {entry.agent} \u00b7 "
            f"{entry.model_id} [{entry.provider}] \u00b7 in={entry.input_tokens:,} "
            f"out={entry.output_tokens:,} \u00b7 {format_cost(entry.cost)} \u00b7 "
            f"run {self._ledger_total_label()}",
            style="dim",
        )

    def _ledger_table(self) -> Table:
        ledger = self._ledger
        assert ledger is not None
        entries = ledger.entries
        shown = entries[-self._LEDGER_MAX_ROWS :]
        hidden = len(entries) - len(shown)
        table = Table(
            title=(
                f"LLM calls this run: {len(entries)} calls \u00b7 "
                f"in={ledger.total_input_tokens:,} out={ledger.total_output_tokens:,} \u00b7 "
                f"{self._ledger_total_label()}"
            ),
            title_justify="left",
            border_style="cyan",
            pad_edge=False,
        )
        table.add_column("#", justify="right", no_wrap=True)
        table.add_column("agent", no_wrap=True)
        table.add_column("model", no_wrap=True)
        table.add_column("provider", no_wrap=True)
        table.add_column("in", justify="right", no_wrap=True)
        table.add_column("out", justify="right", no_wrap=True)
        table.add_column("cache", justify="right", no_wrap=True)
        table.add_column("$", justify="right", no_wrap=True)
        if hidden:
            table.add_row("\u2026", f"{hidden} earlier calls", "", "", "", "", "", "")
        for entry in shown:
            table.add_row(
                str(entry.sequence),
                entry.agent,
                entry.model_id,
                entry.provider,
                f"{entry.input_tokens:,}",
                f"{entry.output_tokens:,}",
                _humanize_cache(entry.cache_read_tokens),
                format_cost(entry.cost),
            )
        table.add_row(
            "total",
            f"{ledger.call_count} calls",
            "",
            "",
            f"{ledger.total_input_tokens:,}",
            f"{ledger.total_output_tokens:,}",
            "",
            self._ledger_total_label(),
            style="bold",
        )
        return table

    def _ledger_total_label(self) -> str:
        assert self._ledger is not None
        return f"${self._ledger.total_cost_usd:.4f}"

    def print_call_ledger(self) -> None:
        """Print the final run ledger (no-op when no ledger is attached)."""
        if self._ledger is not None and self._ledger.entries:
            self._console.print(self._ledger_table())

    def _render_objective_rejected(self, event: FrameworkTraceEvent) -> None:
        data = event.data
        recovery = data.get("recovery")
        maximum = data.get("max")
        suffix = f" ({recovery}/{maximum})" if recovery is not None else ""
        self._console.print(
            Panel(
                Text(
                    "The model ended the active objective with text only and no native "
                    f"tool action. The runtime re-enters the model{suffix}.",
                    overflow="fold",
                ),
                title=Text(f"{event.name} prose stop rejected"),
                border_style="dim yellow",
            )
        )

    def _render_recovery(self, event: FrameworkTraceEvent) -> None:
        data = event.data
        attempt = data.get("attempt")
        error_type = data.get("error_type")
        error = data.get("error")
        detail = error if isinstance(error, str) and error else ""
        if error_type is not None and not detail:
            detail = str(error_type)
        if event.event == "recovery_started":
            title = Text(f"{event.name} recovery started")
            border_style = "yellow"
            body = (
                Text(
                    f"attempt {attempt}: {detail}",
                    overflow="fold",
                )
                if detail
                else Text(f"attempt {attempt}", overflow="fold")
            )
        elif event.event == "recovery_completed":
            title = Text(f"{event.name} recovery recovered")
            border_style = "green"
            body = Text(f"attempt {attempt}", overflow="fold")
        else:
            title = Text(f"{event.name} recovery exhausted")
            border_style = "red"
            body = (
                Text(
                    f"attempt {attempt}: {detail}",
                    overflow="fold",
                )
                if detail
                else Text(f"attempt {attempt}", overflow="fold")
            )
        self._console.print(Panel(body, title=title, border_style=border_style))

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


def _render_tool_call_line(call: dict[str, Any]) -> str:
    name = str(call.get("name") or "tool")
    args = str(call.get("args") or "")
    if len(args) > 160:
        args = args[:157] + "..."
    return f"{name}({args})" if args else name


def _preview(value: Any, limit: int = 240) -> str:
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
