from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from typing import cast

from z_apply_core import __version__
from z_apply_core.agents.model_provider import default_provider_name, list_providers
from z_apply_core.config import load_settings
from z_apply_core.graph import run_job
from z_apply_core.logging_config import configure_logging
from z_apply_core.rich_stream import RichStreamRenderer

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
    renderer = RichStreamRenderer()
    configure_logging(renderer.console)
    try:
        state, result = asyncio.run(
            run_job(
                args.job_url,
                task=args.task,
                live_view=not args.no_vnc,
                sink=renderer,
                provider_name=args.provider,
            )
        )
        renderer.print_result(result, state)
    except KeyboardInterrupt:
        renderer.console.print("[yellow]Run interrupted; resources closed.[/yellow]")
        return 130
    finally:
        renderer.close()
    return 0 if state.get("run_status") == "completed" else 2


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
    try:
        renderer.console.print(table)
    finally:
        renderer.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], int], args.handler)
    return handler(args)
