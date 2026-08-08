from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable
from typing import cast

from rich.text import Text

from z_apply_core import __version__
from z_apply_core.agents.model_provider import default_provider_name, list_providers
from z_apply_core.config import load_settings
from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.context.token_metric import TokenUsage
from z_apply_core.graph import run_job
from z_apply_core.logging_config import configure_logging
from z_apply_core.rich_stream import RichStreamRenderer
from z_apply_core.runtime import RunRuntime
from z_apply_core.state import RunState

logger = logging.getLogger(__name__)

DEFAULT_RUN_TASK = (
    "Complete and submit the current job application: enter the form if needed, "
    "upload the resume early when the primary resume/CV control is available, reuse "
    "known candidate and saved-profile facts, ask the human only for unavailable "
    "answers, and fill the remaining fields in verified semantic operations. Defer a "
    "final-submit CAPTCHA while other work remains. When review-ready, request human "
    "submission approval. If approved, activate final submit and verify visible "
    "submission confirmation."
)
DEFAULT_JOB_URL = "https://amberstudent.keka.com/careers/jobdetails/133388"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="z-apply")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run")
    run.add_argument("--job-url", default=DEFAULT_JOB_URL)
    run.add_argument("--task", default=DEFAULT_RUN_TASK)
    run.add_argument("--provider", choices=[p.name for p in list_providers()])
    run.add_argument("--no-vnc", action="store_true")
    run.set_defaults(handler=run_command)

    providers = subcommands.add_parser(
        "providers",
        help="list available model providers and the active default",
    )
    providers.set_defaults(handler=providers_command)

    return parser


def run_command(args: argparse.Namespace) -> int:
    ledger = RunCallLedger(job_url=args.job_url)
    renderer = RichStreamRenderer(ledger=ledger)
    configure_logging(renderer.console)
    state: RunState | None = None
    interrupted = False
    try:
        state, result = asyncio.run(
            run_job(
                args.job_url,
                task=args.task,
                live_view=not args.no_vnc,
                sink=renderer,
                provider_name=args.provider,
                call_ledger=ledger,
            )
        )
        renderer.print_result(result, state)
        _print_token_usage(renderer, state)
    except KeyboardInterrupt:
        interrupted = True
        renderer.console.print("[yellow]Run interrupted; resources closed.[/yellow]")
    finally:
        # The ledger must always surface, even on Ctrl+C or a crash: it is
        # the only record of what the run spent.
        renderer.print_call_ledger()
        status = "interrupted" if interrupted else _run_status(state)
        _persist_call_ledger(ledger, state, status)
    if interrupted:
        return 130
    return 0 if _run_status(state) == "completed" else 2


def _run_status(state: RunState | None) -> str:
    if state is None:
        return ""
    return str(state.get("run_status", ""))


def _persist_call_ledger(
    ledger: RunCallLedger,
    state: RunState | None,
    status: str,
) -> None:
    """Persist the run's LLM ledger so it survives process shutdown."""
    from z_apply_core.config import CORE_ROOT

    run_id = ""
    if state is not None:
        runtime = state.get("runtime")
        if isinstance(runtime, RunRuntime):
            run_id = str(runtime.run_id or "")
    history_path, run_copy = ledger.write_history(
        CORE_ROOT / ".z-apply",
        run_id=run_id,
        status=status,
    )
    paths = [str(history_path)]
    if run_copy is not None:
        paths.append(str(run_copy))
    logger.info("LLM call ledger saved: %s", ", ".join(paths))


def _print_token_usage(renderer: RichStreamRenderer, state: RunState) -> None:
    usage = _last_token_usage(state)
    if usage is None:
        return
    renderer.console.print(
        Text(
            "token usage: "
            f"prompt_tokens={usage.prompt_tokens} "
            f"completion_tokens={usage.completion_tokens} "
            f"cache_read_tokens={usage.cache_read_tokens} "
            f"tool_schema_tokens={usage.tool_schema_tokens} "
            f"messages={usage.message_count} "
            f"tools={usage.tool_count}",
            style="dim",
        )
    )


def _last_token_usage(state: RunState) -> TokenUsage | None:
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime):
        return None
    run_context = runtime.browser.run_context
    if run_context is None:
        return None
    return run_context.usage_totals or run_context.token_usage


def providers_command(_args: argparse.Namespace) -> int:
    from rich.table import Table

    settings = load_settings()
    default = default_provider_name()

    table = Table(title="Available model providers")
    table.add_column("Provider")
    table.add_column("Default model")
    table.add_column("Status")
    table.add_column("Configure via")

    for spec in list_providers():
        model = spec.default_model or "router-managed"
        configure_via = spec.env_key or "router instance"
        if spec.env_attr:
            status = "no key set"
            if getattr(settings, spec.env_attr):
                status = "key set"
        else:
            status = "needs router"
        if spec.name == default:
            status = "active default"
        table.add_row(spec.name, model, status, configure_via)

    renderer = RichStreamRenderer()
    renderer.console.print(table)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], int], args.handler)
    return handler(args)
