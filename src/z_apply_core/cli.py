from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from rich.text import Text

from z_apply_core import __version__
from z_apply_core.agents.model_provider import (
    default_provider_name,
    get_provider,
    list_providers,
)
from z_apply_core.config import load_settings
from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.context.model_metrics import extract_cache_read, extract_usage_dict
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
    run.add_argument("--prompt-variant", default=None, help="orchestrator prompt variant filename (e.g. orchestrator-v2.md)")
    run.set_defaults(handler=run_command)

    providers = subcommands.add_parser(
        "providers",
        help="list available model providers and the active default",
    )
    providers.set_defaults(handler=providers_command)

    probe = subcommands.add_parser(
        "cache-probe",
        help="verify the provider's prompt cache: sends one fixed prompt twice and "
        "checks the second call reports cache_read_tokens",
    )
    probe.add_argument("--provider", choices=[p.name for p in list_providers()])
    probe.set_defaults(handler=cache_probe_command)

    return parser


def run_command(args: argparse.Namespace) -> int:
    from z_apply_core.agents.prompts import prompt_sha, resolve_orchestrator_prompt

    variant = resolve_orchestrator_prompt(args.prompt_variant)
    ledger = RunCallLedger(job_url=args.job_url, prompt_variant=variant, prompt_sha=prompt_sha(variant))
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
                prompt_variant=variant,
                prompt_sha=ledger.total_sha,
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
        if state is not None:
            ledger.set_terminal_reason(str(state.get("orchestrator_summary", "") or ""))
        elif not interrupted:
            # The run crashed before any state was produced; keep the record
            # queryable with an explicit sentinel instead of an empty string.
            ledger.set_terminal_reason("unavailable: no run state produced")
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


_CACHE_PROBE_SYSTEM = (
    "You are a prefix-cache probe. Respond with the single word `ok`. "
    "Do not add anything else."
)
_CACHE_PROBE_USER = (
    "This exact prompt is sent twice to verify the provider's prompt cache. "
    "Reply with the single word `ok`."
)


async def _cache_probe_once(model: Any) -> tuple[int | None, int | None, float]:
    """Send the fixed probe prompt once; return (cache_read, input, seconds)."""

    @tool
    async def probe_tool(value: str) -> str:
        """A fixed tool schema so the probe matches the shape of a real agent call."""
        return value

    bound = model.bind_tools([probe_tool])
    started = time.monotonic()
    result = await bound.ainvoke(
        [
            SystemMessage(content=_CACHE_PROBE_SYSTEM),
            HumanMessage(content=_CACHE_PROBE_USER),
        ]
    )
    usage = extract_usage_dict(result) or {}
    input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
    return (
        extract_cache_read(usage),
        int(input_tokens) if isinstance(input_tokens, int) else None,
        time.monotonic() - started,
    )


def cache_probe_command(args: argparse.Namespace) -> int:
    """Verify the configured provider reports cache reads on a repeated prompt.

    The same fixed prompt (with a tool schema bound, the shape of a real agent
    call) is sent twice. The second call must report ``cache_read_tokens > 0``:
    a fully cached second call proves the prefix cache is working, while a
    second-call miss means every repeated prefix is billed at the full input
    rate instead of the discounted cache-read rate.
    """
    provider_name = args.provider or default_provider_name()
    try:
        provider = get_provider(provider_name=provider_name or None)
        selection = asyncio.run(provider.lease(tools=True, reasoning=False))
    except Exception as exc:
        print(f"cache-probe: could not lease a model from {provider_name or 'default'}: {exc}")
        return 2

    try:
        first = asyncio.run(_cache_probe_once(selection.llm))
        second = asyncio.run(_cache_probe_once(selection.llm))
    except Exception as exc:
        print(f"cache-probe: model call failed: {type(exc).__name__}: {exc}")
        return 2

    cache1, input1, seconds1 = first
    cache2, input2, seconds2 = second
    print(f"provider={provider_name or 'default'}")
    print(f"model={selection.info.id}")
    print(
        f"call 1 (cold):      input={input1 if input1 is not None else 'n/a':>6} "
        f"cache_read={cache1 if cache1 is not None else 'n/a'} "
        f"({seconds1:.2f}s)"
    )
    if cache2 is None:
        print(
            "call 2:             cache_read=n/a -- provider did not report cache "
            "usage (cannot verify; inspect the raw usage response)"
        )
        return 1
    ratio = (cache2 / input2 * 100.0) if input2 and input2 > 0 else 0.0
    print(
        f"call 2 (repeat):    input={input2 if input2 is not None else 'n/a':>6} "
        f"cache_read={cache2} ({ratio:.0f}% of input) ({seconds2:.2f}s)"
    )
    if cache2 > 0:
        print("PASS: prefix cache is working; repeated prefixes are billed as cache reads.")
        return 0
    print(
        "FAIL: the second call reported zero cache reads. The provider/gateway is "
        "not caching this prompt prefix -- every repeated prefix is billed at the "
        "full input rate. Check prompt_cache_key/retention and cache_control "
        "support on the gateway, or switch to a provider with native prefix caching."
    )
    return 1


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
        model = spec.default_model
        configure_via = spec.env_key
        status = "key set" if getattr(settings, spec.env_attr, False) else "no key set"
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
