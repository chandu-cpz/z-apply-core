"""Continuously apply to jobs through the backend, cheaply and sequentially.

Reads job URLs from a jobs file (one per line, ``#`` comments allowed, optional
``URL | task`` syntax), POSTs each to the local backend as a run, waits for
slot capacity, and records per-run + total cost from the backend's call
ledger. It skips jobs it has already attempted (state file) so restarts never
re-apply to the same job, and it keeps running when new lines are appended to
the jobs file (``--watch``, the default).

The backend queues runs itself (up to ``Z_APPLY_MAX_ACTIVE_RUNS``, default 3
concurrently), so this runner only needs to stay within capacity. Runs appear
in the cockpit at http://127.0.0.1:5173; human approvals (final submit,
CAPTCHA) are answered there — the runner never auto-approves anything.

Usage:
    uv run python scripts/continuous_apply.py --once          # drain the list, then exit
    uv run python scripts/continuous_apply.py --watch         # keep watching jobs.txt (default)
    uv run python scripts/continuous_apply.py --jobs-file path/to/jobs.txt --force

Options:
    --jobs-file     job list file (default: z-apply-core/jobs.txt)
    --api           backend base URL (default http://127.0.0.1:8000)
    --max-active    do not queue beyond this many active runs (default: the
                      backend's Z_APPLY_MAX_ACTIVE_RUNS, read from /diagnostics)
    --interval      poll seconds (default 30)
    --once          drain the job list and exit
    --force         re-attempt jobs already in the state file
    --resume-interrupted  re-queue backend runs the last process left interrupted
                      (a restart mid-application). Off by default: the backend
                      deliberately never auto-retries interrupted runs because a
                      crash could have landed the submit click, so only enable
                      this when you are sure no submission went through.
    --state-file    progress/cost state (default .z-apply/continuous-state.json)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import z_apply_core  # noqa: F401  (ensure z_apply_core on path when run via uv)

DEFAULT_JOBS_FILE = Path(__file__).resolve().parent.parent / "jobs.txt"
DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / ".z-apply" / "continuous-state.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="continuous_apply", description=__doc__)
    parser.add_argument("--jobs-file", default=str(DEFAULT_JOBS_FILE))
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--max-active", type=int, default=None)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--resume-interrupted", action="store_true")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    return parser.parse_args(argv)


def _read_jobs(path: Path) -> list[dict[str, str]]:
    """Parse the jobs file into ``[{url, task}]`` entries."""
    jobs: list[dict[str, str]] = []
    if not path.exists():
        print(f"continuous: jobs file not found: {path}", file=sys.stderr)
        return jobs
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url, _, task = line.partition("|")
        url = url.strip()
        task = task.strip() or ""
        if url:
            jobs.append({"url": url, "task": task})
    return jobs


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _backend_max_active(api: str) -> int:
    """The backend's configured concurrency (Z_APPLY_MAX_ACTIVE_RUNS).

    The backend is the single authority for how many runs may execute at
    once; the runner mirrors it so it never queues beyond what the scheduler
    will start. Falls back to 3 when diagnostics are unreachable.
    """
    try:
        diagnostics = _http_json("GET", f"{api}/api/v1/diagnostics")
        return int(diagnostics.get("max_active_runs") or 3)
    except Exception:
        return 3


def _active_runs(api: str) -> list[dict[str, Any]]:
    """Runs currently queued, starting, running, or waiting on the backend.

    ``waiting_human`` holds a browser and a scheduler slot, so it counts
    toward capacity exactly like a running run.
    """
    rows = _http_json("GET", f"{api}/api/v1/runs?limit=100") or []
    return [r for r in rows if r.get("status") not in ("terminal",)]


def _active_urls(api: str) -> set[str]:
    """Job URLs already queued/running/waiting on the backend.

    Guards against duplicate concurrent applications: a job that is already
    in flight (e.g. re-queued by --force or by an earlier runner instance)
    must not get a second run on the same URL until the first is terminal.
    """
    return {str(r.get("job_url", "")) for r in _active_runs(api)}


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"attempted": {}, "total_cost_usd": 0.0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"attempted": {}, "total_cost_usd": 0.0}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _run_ledger(api: str, run_id: str) -> dict[str, float | int]:
    """Per-run model-call totals from the backend ledger.

    Returns ``{"cost_usd": ..., "calls": ...}`` on success and an empty
    dict on failure so callers never persist a wrong zero on a transient
    API error (a later drain can re-fetch).
    """
    try:
        calls = _http_json("GET", f"{api}/api/v1/runs/{run_id}/calls")
        totals = calls.get("totals") or {}
        return {
            "cost_usd": float(totals.get("cost_usd") or 0.0),
            "calls": int(totals.get("calls") or 0),
        }
    except Exception:
        return {}


def _run_cost(api: str, run_id: str) -> float:
    """Per-run cost from the backend's model-call ledger (0 if unknown)."""
    return float(_run_ledger(api, run_id).get("cost_usd") or 0.0)


def _is_retryable_failure(entry: dict[str, Any]) -> bool:
    """A prior attempt that failed before any model call is retryable.

    The backend marks all start/setup failures (pool capacity, browser
    launch) with a generic ``failed`` outcome and zero model calls — the run
    never got a fair shot at the form, so re-queueing it is safe. A failed
    run with calls means the agent crashed AFTER work (possibly after the
    submit click), so it is NOT auto-retried: re-apply only with ``--force``
    to avoid duplicate real submissions. `calls` is None for entries recorded
    before ledger draining existed; treat as 0.
    """
    calls = entry.get("calls")
    return entry.get("status") == "failed" and (calls is None or int(calls) == 0)


def _start_run(api: str, job: dict[str, str]) -> dict[str, Any]:
    body: dict[str, Any] = {"job_url": job["url"]}
    if job.get("task"):
        body["task"] = job["task"]
    return _http_json("POST", f"{api}/api/v1/runs", body)


def _drain_finished(api: str, state: dict[str, Any]) -> float:
    """Fold terminal runs into the cost state; return the new total."""
    total = float(state.get("total_cost_usd") or 0.0)
    rows = _http_json("GET", f"{api}/api/v1/runs?limit=100") or []
    for row in rows:
        if row.get("status") != "terminal":
            continue
        run_id = str(row.get("id", ""))
        entry = state["attempted"].get(run_id)
        if entry is None:
            continue
        if "cost_usd" in entry and "status" in entry and "calls" in entry:
            continue
        entry["status"] = row.get("outcome") or row.get("status")
        entry["terminal_detail"] = str(row.get("summary") or "")
        ledger = _run_ledger(api, run_id)
        if not ledger:
            continue  # transient API error: leave the entry incomplete to re-fetch later
        entry["calls"] = ledger["calls"]
        entry["cost_usd"] = ledger["cost_usd"]
        if ledger["cost_usd"]:
            total = float(state.get("total_cost_usd") or 0.0) + ledger["cost_usd"]
            state["total_cost_usd"] = round(total, 6)
    return total


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    jobs_file = Path(args.jobs_file)
    state_path = Path(args.state_file)
    state = _load_state(state_path)
    attempted_by_url = {
        entry.get("url"): run_id for run_id, entry in state["attempted"].items() if entry.get("url")
    }
    forced_once = False
    # Single concurrency authority: the backend's Z_APPLY_MAX_ACTIVE_RUNS
    # (unless the operator overrides with --max-active).
    max_active = args.max_active or _backend_max_active(args.api)

    print(
        f"continuous: api={args.api} jobs={jobs_file} max_active={max_active} watch={not args.once}"
    )
    print(
        f"continuous: total spent so far ${float(state.get('total_cost_usd') or 0.0):.4f} "
        f"({len(state['attempted'])} runs attempted)"
    )

    while True:
        total = _drain_finished(args.api, state)
        jobs = _read_jobs(jobs_file)
        # ``--force`` re-applies every known job exactly ONCE, then behaves
        # like a normal watch: without this, force + watch would re-POST every
        # job (and re-request fresh approvals) on every interval loop.
        force_this_pass = bool(args.force) and not forced_once
        if force_this_pass:
            forced_once = True

        # Resume backend runs the last process left interrupted (opt-in).
        # Each interrupted run is resumed at most once: pop the URL only when
        # this runner still maps it to that exact interrupted run (a fresh run
        # for the same URL means it was already resumed) and the URL is not
        # already in flight.
        if args.resume_interrupted:
            rows = _http_json("GET", f"{args.api}/api/v1/runs?limit=100") or []
            active_urls_now = {
                str(r.get("job_url", ""))
                for r in rows
                if r.get("status") in ("queued", "starting", "running", "waiting_human")
            }
            for row in rows:
                if row.get("outcome") != "interrupted":
                    continue
                url = str(row.get("job_url", ""))
                old_id = str(row.get("id", ""))
                if url in active_urls_now:
                    continue  # a fresh run for this URL is already in flight
                if attempted_by_url.get(url) == old_id or old_id in state["attempted"]:
                    if old_id in state["attempted"]:
                        state["attempted"][old_id]["status"] = "resumed"
                    if attempted_by_url.get(url) == old_id:
                        attempted_by_url.pop(url, None)
                        print(f"continuous: re-apply to interrupted job {url} (old run {old_id})")

        pending = [
            j
            for j in jobs
            if (
                force_this_pass
                or j["url"] not in attempted_by_url
                or _is_retryable_failure(state["attempted"].get(attempted_by_url[j["url"]], {}))
            )
        ]
        active_urls = _active_urls(args.api)
        pending = [j for j in pending if j["url"] not in active_urls]
        in_flight = sum(1 for j in jobs if j["url"] in active_urls)
        if in_flight:
            print(f"continuous: {in_flight} job(s) already in flight; skipping duplicates")
        if not pending:
            if args.once:
                _save_state(state_path, state)
                print("continuous: no pending jobs; done (--once).")
                return 0
            print(f"continuous: no pending jobs; watching {jobs_file} for new entries…")
            time.sleep(args.interval)
            continue

        active = _active_runs(args.api)
        print(
            f"continuous: {len(pending)} pending job(s), {len(active)} active run(s) "
            f"(cost so far ${total:.4f})"
        )
        for job in pending:
            if len(_active_runs(args.api)) >= max_active:
                print("continuous: capacity full; waiting for a slot…")
                break
            try:
                run = _start_run(args.api, job)
            except Exception as exc:
                # Transient POST failure: leave the URL pending (do NOT mark it
                # attempted) so the next interval loop re-tries it. The interval
                # provides natural backoff.
                print(
                    f"continuous: failed to start {job['url']}: {exc}; will retry", file=sys.stderr
                )
                continue
            run_id = str(run.get("id", ""))
            state["attempted"][run_id] = {
                "url": job["url"],
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            attempted_by_url[job["url"]] = run_id
            print(f"continuous: queued {job['url']} -> run {run_id}")
            _save_state(state_path, state)
            time.sleep(2)  # brief spacing between POSTs so the queue drains cleanly

        if args.once:
            # wait for the queued batch to finish, then report final cost
            while _active_runs(args.api):
                time.sleep(args.interval)
            total = _drain_finished(args.api, state)
            _save_state(state_path, state)
            print(
                f"continuous: batch finished; total spent ${total:.4f} across "
                f"{len(state['attempted'])} runs."
            )
            return 0

        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
