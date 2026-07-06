from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from typing import cast

from z_apply_core import __version__
from z_apply_core.graph import run_job
from z_apply_core.rich_stream import RichStreamRenderer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="z-apply")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run")
    run.add_argument("--job-url", required=True)
    run.add_argument("--no-vnc", action="store_true")
    run.set_defaults(handler=run_command)

    return parser


def run_command(args: argparse.Namespace) -> int:
    renderer = RichStreamRenderer()
    state, result = asyncio.run(
        run_job(args.job_url, live_view=not args.no_vnc, sink=renderer)
    )
    renderer.print_result(result, state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], int], args.handler)
    return handler(args)
