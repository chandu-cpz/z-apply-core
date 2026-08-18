from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from z_apply_core.context.cost_estimate import CostEstimate, estimate_cost


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One successful model call recorded for a run."""

    sequence: int
    agent: str
    model_id: str
    provider: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    ttft_ms: int | None = None
    duration_ms: int | None = None
    gateway_cost_usd: float | None = None
    cost: CostEstimate = CostEstimate(usd=0.0)


class RunCallLedger:
    """Per-run list of successful LLM calls with running token/cost totals.

    One ledger exists per CLI run, which is one job URL, so its scope is
    exactly "all model calls made for this job URL so far". Records are
    appended at the router boundary after each successful model call, using
    real usage tokens when the provider reports them and the call-start
    estimate otherwise.
    """

    def __init__(self, *, job_url: str = "", prompt_variant: str | None = None, prompt_sha: str | None = None) -> None:
        self._job_url = job_url
        self._prompt_variant = prompt_variant
        self._prompt_sha = prompt_sha
        self._terminal_reason: str = ""
        self._entries: list[LedgerEntry] = []
        self._total_input = 0
        self._total_output = 0
        self._total_cost = 0.0

    @property
    def job_url(self) -> str:
        return self._job_url

    def set_prompt_identity(self, variant: str, sha: str) -> None:
        """Record which orchestrator prompt variant this run executed."""
        self._prompt_variant = variant
        self._prompt_sha = sha

    def set_terminal_reason(self, reason: str) -> None:
        """Record the terminal reason (block reason or submit confirmation).

        Called after the run ends so each ledger record carries *why* the run
        stopped — the raw material for comparing prompt variants and mining
        failure modes without digging through observation files.
        """
        self._terminal_reason = reason

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    @property
    def call_count(self) -> int:
        return len(self._entries)

    @property
    def total_input_tokens(self) -> int:
        return self._total_input

    @property
    def total_output_tokens(self) -> int:
        return self._total_output

    @property
    def total_cost_usd(self) -> float:
        return self._total_cost

    @property
    def total_sha(self) -> str:
        return self._prompt_sha or ""

    def record(
        self,
        *,
        agent: str,
        model_id: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        ttft_ms: int | None = None,
        duration_ms: int | None = None,
        gateway_cost_usd: float | None = None,
    ) -> LedgerEntry:
        """Append one successful call and update the running totals.

        A gateway-reported dollar cost wins over the rate-card estimate — even
        ``0.0``, which is authoritative for subscription-covered/free requests
        (the OpenCode Go gateway reports ``cost: "0"`` on every covered call).
        The estimate only fills the entry when the gateway reports no cost, so
        totals stay meaningful.
        """
        estimate = estimate_cost(
            provider,
            max(0, input_tokens),
            max(0, output_tokens),
            max(0, cache_read_tokens),
        )
        cost = (
            CostEstimate(usd=round(gateway_cost_usd, 6))
            if gateway_cost_usd is not None
            else estimate
        )
        entry = LedgerEntry(
            sequence=len(self._entries) + 1,
            agent=agent,
            model_id=model_id,
            provider=provider,
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            cache_read_tokens=max(0, cache_read_tokens),
            ttft_ms=ttft_ms,
            duration_ms=duration_ms,
            gateway_cost_usd=(round(gateway_cost_usd, 6) if gateway_cost_usd is not None else None),
            cost=cost,
        )
        self._entries.append(entry)
        self._total_input += entry.input_tokens
        self._total_output += entry.output_tokens
        self._total_cost += entry.cost.usd
        return entry

    def to_record(self, *, run_id: str = "", status: str = "") -> dict[str, object]:
        """Serialize the ledger for persistence (one record per run)."""
        return {
            "run_id": run_id,
            "job_url": self._job_url,
            "prompt_variant": getattr(self, "_prompt_variant", None),
            "prompt_sha": getattr(self, "_prompt_sha", None),
            "status": status,
            "terminal_reason": getattr(self, "_terminal_reason", ""),
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "calls": [
                {
                    "sequence": entry.sequence,
                    "agent": entry.agent,
                    "model": entry.model_id,
                    "provider": entry.provider,
                    "input_tokens": entry.input_tokens,
                    "output_tokens": entry.output_tokens,
                    "cache_read_tokens": entry.cache_read_tokens,
                    "ttft_ms": entry.ttft_ms,
                    "duration_ms": entry.duration_ms,
                    "cost_usd": entry.cost.usd,
                    "gateway_cost_usd": entry.gateway_cost_usd,
                }
                for entry in self._entries
            ],
            "totals": {
                "calls": self.call_count,
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "cost_usd": round(self.total_cost_usd, 6),
            },
        }

    def write_history(
        self,
        base_dir: Path,
        *,
        run_id: str = "",
        status: str = "",
    ) -> tuple[Path, Path | None]:
        """Persist this run's ledger beyond process shutdown.

        Appends one JSON record to ``<base_dir>/llm-ledger.jsonl`` (queryable
        per job URL across runs) and writes a per-run copy into
        ``<base_dir>/runs/<run_id>/call-ledger.json`` when a run id is known.
        Returns the written paths.
        """
        base_dir.mkdir(parents=True, exist_ok=True)
        history_path = base_dir / "llm-ledger.jsonl"
        record = self.to_record(run_id=run_id, status=status)
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        run_copy: Path | None = None
        if run_id:
            run_copy = base_dir / "runs" / run_id / "call-ledger.json"
            run_copy.parent.mkdir(parents=True, exist_ok=True)
            run_copy.write_text(
                json.dumps(record, indent=2, default=str),
                encoding="utf-8",
            )
        return history_path, run_copy
