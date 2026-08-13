"""Compare orchestrator prompt variants on the same job.

Runs the identical job (same URL, task, resume, profile) under each selected
prompt variant and repeats, then prints a comparison table of the metrics that
matter for prompt experiments: terminal status, call count, tokens, cost, and
duration. Every run records ``prompt_variant`` + ``prompt_sha`` + the terminal
reason, so runs are auditable and comparable.

SAFETY: an eval-only human channel is installed that (a) raises on every
``ask_human`` so no fabricated candidate fact is ever stored in candidate
memory, and (b) declines every submission-approval request, so an eval run can
NEVER submit a real application. Runs that reach the submission-review gate
stop there — that is the measurable "form completed" proxy.

Usage:
    uv run python scripts/eval_prompts.py --job-url <URL> [options]
    uv run python scripts/eval_prompts.py --list

Examples:
    # compare the two most distinct variants, twice each
    uv run python scripts/eval_prompts.py \
        --job-url "https://job-boards.greenhouse.io/insurityindia/jobs/4354932009" \
        --variants orchestrator-minimal.md,orchestrator-outcome.md --repeats 2

    # all variants, one run each, against the CLI default job
    uv run python scripts/eval_prompts.py --job-url "$ZAPPLY_JOB_URL" --repeats 1

Options:
    --job-url        job to apply to (default: the CLI default job)
    --task           objective text (default: the CLI default run task)
    --variants       comma-separated variant filenames; omit = all variants
    --repeats        runs per variant (default 1)
    --provider       model provider name (default: active default)
    --list           list available variants and exit without running
    --out            write the comparison table + per-run rows to this JSON file
    --no-vnc         run without the live VNC view (default already off)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from z_apply_core.agents.prompts import (
    DEFAULT_ORCHESTRATOR_PROMPT,
    list_prompt_variants,
    prompt_sha,
    resolve_orchestrator_prompt,
)
from z_apply_core.cli import DEFAULT_RUN_TASK
from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.graph import run_job

logger = logging.getLogger(__name__)


class EvalHumanChannel:
    """Human-channel stand-in that makes eval runs safe and deterministic.

    - ``ask`` raises, so the ``ask_human`` tool takes its error path
      (``human_answer=""`` + error) and NEVER stores a fabricated value in
      candidate memory. Runs that need a missing fact proceed and terminate
      cleanly via the normal blocker machinery.
    - ``confirm`` always declines, so the SubmissionReviewer can never perform
      the real submit click. Reaching the approval gate is the eval's success
      proxy; the run stops there instead of submitting a live application.
    """

    async def send_artifact(self, *, path: str, caption: str) -> None:
        return None

    async def ask(
        self,
        *,
        question: str,
        context: str = "",
        url: str = "",
        company: str = "System",
        role: str = "Application",
        options: list[str] | None = None,
        risk: str = "medium",
        image_path: str = "",
        field_label: str = "",
        reason: str = "",
    ) -> str:
        raise RuntimeError(
            "eval-prompt run has no human: the run must resolve fields from "
            "memory/resume evidence or terminate cleanly."
        )

    async def confirm(
        self,
        *,
        question: str,
        context: str = "",
        url: str = "",
        company: str = "System",
        role: str = "Application",
    ) -> bool:
        # Never approve a real submission from an eval run.
        return False


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eval_prompts",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--job-url", default="", help="job URL to run against")
    parser.add_argument("--task", default=DEFAULT_RUN_TASK, help="run objective")
    parser.add_argument(
        "--variants",
        default="",
        help="comma-separated variant filenames; empty = every variant",
    )
    parser.add_argument("--repeats", type=int, default=1, help="runs per variant")
    parser.add_argument("--provider", default=None, help="model provider name")
    parser.add_argument("--list", action="store_true", help="list variants and exit")
    parser.add_argument("--out", default="", help="JSON output path")
    parser.add_argument("--no-vnc", action="store_true", help="disable live view")
    parser.add_argument("--vnc", action="store_true", help="enable the live VNC view (default off for eval)")
    return parser.parse_args(argv)


def _resolve_variants(raw: str) -> list[str]:
    """Resolve the --variants argument to loadable prompt filenames."""
    variants = list_prompt_variants()
    if not raw.strip():
        return list(variants)
    resolved: list[str] = []
    for name in (part.strip() for part in raw.split(",")):
        if not name:
            continue
        if name == "default":
            name = DEFAULT_ORCHESTRATOR_PROMPT
        resolved_name = resolve_orchestrator_prompt(name)
        if resolved_name != DEFAULT_ORCHESTRATOR_PROMPT or name == DEFAULT_ORCHESTRATOR_PROMPT:
            resolved.append(resolved_name)
        else:
            raise SystemExit(f"unknown prompt variant: {name!r} (available: {', '.join(variants)})")
    return resolved


def _print_variants() -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="Orchestrator prompt variants")
    table.add_column("Variant")
    table.add_column("sha256")
    table.add_column("Default")
    default = resolve_orchestrator_prompt(None)
    for name in list_prompt_variants():
        table.add_row(name, prompt_sha(name)[:12], "yes" if name == default else "")
    console.print(table)


async def _run_once(
    *,
    job_url: str,
    task: str,
    variant: str,
    provider: str | None,
    live_view: bool,
) -> dict[str, Any]:
    """Run one job under one variant; return the comparison row."""
    from unittest.mock import patch

    from z_apply_core.agents.model_provider import get_provider
    from z_apply_core.agents.prompts import prompt_sha

    sha = prompt_sha(variant)
    ledger = RunCallLedger(job_url=job_url, prompt_variant=variant, prompt_sha=sha)
    started = time.monotonic()
    state: dict[str, Any] | None = None
    error = ""
    try:
        with patch(
            "z_apply_core.nodes.setup_browser.make_configured_human_channel",
            return_value=EvalHumanChannel(),
        ):
            state, _result = await run_job(
                job_url,
                task=task,
                live_view=live_view,
                prompt_variant=variant,
                prompt_sha=sha,
                provider=get_provider(provider_name=provider or None),
                call_ledger=ledger,
            )
    except Exception as exc:  # noqa: BLE001 - one failed run must not kill the batch
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("eval run failed for variant %s", variant)
    duration_s = time.monotonic() - started

    status = ""
    terminal_reason = ""
    if state is not None:
        status = str(state.get("run_status", "") or "")
        terminal_reason = str(state.get("orchestrator_summary", "") or "")
        ledger.set_terminal_reason(terminal_reason)
    if error:
        status = status or "errored"
        terminal_reason = terminal_reason or error

    return {
        "variant": variant,
        "prompt_sha": sha,
        "status": status,
        "terminal_reason": terminal_reason[:500],
        "calls": ledger.call_count,
        "input_tokens": ledger.total_input_tokens,
        "output_tokens": ledger.total_output_tokens,
        "cache_read_tokens": sum(e.cache_read_tokens for e in ledger.entries),
        "cost_usd": round(ledger.total_cost_usd, 6),
        "duration_s": round(duration_s, 1),
    }


async def _run_batch(args: argparse.Namespace) -> list[dict[str, Any]]:
    from z_apply_core.agents.model_provider import default_provider_name

    job_url = args.job_url or _cli_default_job_url()
    provider = args.provider or default_provider_name()
    variants = _resolve_variants(args.variants)
    print(
        f"eval: job={job_url} provider={provider} "
        f"variants={len(variants)} repeats={args.repeats}"
    )
    rows: list[dict[str, Any]] = []
    total = len(variants) * max(1, args.repeats)
    index = 0
    for variant in variants:
        for repeat in range(max(1, args.repeats)):
            index += 1
            print(f"--- [{index}/{total}] variant={variant} repeat={repeat + 1} ---", flush=True)
            row = await _run_once(
                job_url=job_url,
                task=args.task,
                variant=variant,
                provider=provider,
                live_view=args.vnc,
            )
            rows.append(row)
            print(
                f"    status={row['status']} calls={row['calls']} "
                f"in={row['input_tokens']} out={row['output_tokens']} "
                f"cost=${row['cost_usd']:.4f} {row['duration_s']:.0f}s",
                flush=True,
            )
            if row["terminal_reason"]:
                print(f"    reason: {row['terminal_reason'][:300]}", flush=True)
    return rows


def _print_table(rows: list[dict[str, Any]]) -> None:
    from collections import Counter, defaultdict

    from rich.console import Console
    from rich.table import Table

    console = Console()
    by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_variant[row["variant"]].append(row)

    table = Table(title="Prompt variant comparison")
    table.add_column("Variant")
    table.add_column("Runs")
    table.add_column("Statuses")
    table.add_column("Calls (avg)")
    table.add_column("Input tok (avg)")
    table.add_column("Output tok (avg)")
    table.add_column("Cost (avg)")
    table.add_column("Duration s (avg)")
    for variant, group in by_variant.items():
        statuses = Counter(r["status"] for r in group)
        status_text = ", ".join(f"{name}={count}" for name, count in statuses.most_common())
        table.add_row(
            variant,
            str(len(group)),
            status_text,
            f"{sum(r['calls'] for r in group) / len(group):.1f}",
            f"{sum(r['input_tokens'] for r in group) / len(group):,.0f}",
            f"{sum(r['output_tokens'] for r in group) / len(group):,.0f}",
            f"${sum(r['cost_usd'] for r in group) / len(group):.4f}",
            f"{sum(r['duration_s'] for r in group) / len(group):.1f}",
        )
    console.print(table)
    print("\nTerminal reasons:")
    for row in rows:
        reason = (row["terminal_reason"] or "(none)").replace("\n", " ")[:220]
        print(f"  [{row['status']:10s}] {row['variant']}: {reason}")


def _cli_default_job_url() -> str:
    from z_apply_core.cli import DEFAULT_JOB_URL

    return DEFAULT_JOB_URL


def _write_out(rows: list[dict[str, Any]], args: argparse.Namespace) -> Path | None:
    if not args.out:
        return None
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_url": args.job_url,
        "task": args.task,
        "provider": args.provider,
        "variants": _resolve_variants(args.variants),
        "repeats": args.repeats,
        "rows": rows,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote results to {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.list:
        _print_variants()
        return 0
    if not args.job_url and not _cli_default_job_url():
        print("eval: no job URL; pass --job-url", file=sys.stderr)
        return 2
    rows = asyncio.run(_run_batch(args))
    _print_table(rows)
    _write_out(rows, args)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
