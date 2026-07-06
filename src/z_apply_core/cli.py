from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from typing import cast

from z_apply_core import __version__
from z_apply_core.graph import run_job
from z_apply_core.logging_config import configure_logging
from z_apply_core.rich_stream import RichStreamRenderer

DEFAULT_RUN_TASK = (
    "Navigate from the job details page to the actual application form. You may click "
    "a safe Apply or Start Application entry point. Do not fill fields, upload files, "
    "click final submit, or perform irreversible application actions."
)
DEFAULT_JOB_URL = "https://amberstudent.keka.com/careers/jobdetails/133388"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="z-apply")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run")
    run.add_argument("--job-url", default=DEFAULT_JOB_URL)
    run.add_argument("--task", default=DEFAULT_RUN_TASK)
    run.add_argument("--no-vnc", action="store_true")
    run.set_defaults(handler=run_command)

    return parser


def run_command(args: argparse.Namespace) -> int:
    renderer = RichStreamRenderer()
    configure_logging(renderer.console)
    try:
        state, result = asyncio.run(
            run_job(args.job_url, task=args.task, live_view=not args.no_vnc, sink=renderer)
        )
        renderer.print_result(result, state)
    finally:
        renderer.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], int], args.handler)
    return handler(args)
