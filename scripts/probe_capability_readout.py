"""Readout for DEC-010 capability_probe telemetry (OPT-DEC-010 verification).

Parses ``capability_probe`` log lines from one or more text files (backend
stdout captures, run logs) and reports:

- probe count + injected/dedupe-skipped split
- H1 cache behaviour: hit-rate, result_hash repetition across turns
- H2 cost: inspection_ms distribution over FRESH scans (cache hits carry the
  original scan's ms by design; counting them would double-count)

Usage:
    uv run python scripts/probe_capability_readout.py /tmp/backend.log [more...]
"""

from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

PROBE_RE = re.compile(
    r"capability_probe\s+revision=(?P<revision>\S+)\s+result_hash=(?P<hash>\S+)\s+"
    r"inspection_ms=(?P<ms>\d+)\s+controls_scanned=(?P<controls>\d+)\s+"
    r"injected=(?P<injected>\S+)(?:\s+cache=(?P<cache>\S+))?"
)


def _iter_lines(sources: list[str]) -> list[str]:
    for source in sources:
        path = Path(source)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    yield from _read(child)
        elif path.exists():
            yield from _read(path)
        else:
            print(f"skip (missing): {source}")


def _read(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError as exc:
        print(f"skip (unreadable): {path}: {exc}")
        return []


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def main() -> None:
    sources = sys.argv[1:]
    if not sources:
        print(__doc__)
        raise SystemExit(2)

    probes = [
        match.groupdict()
        for line in _iter_lines(sources)
        if (match := PROBE_RE.search(line))
    ]
    if not probes:
        print("no capability_probe lines found")
        raise SystemExit(1)

    total = len(probes)
    injected = sum(1 for p in probes if p["injected"] == "true")
    hits = sum(1 for p in probes if p.get("cache") == "hit")
    fresh = sum(1 for p in probes if p.get("cache") in {None, "miss", "fresh"})
    errors = sum(1 for p in probes if p.get("cache") == "error")

    hash_counts: dict[str, int] = {}
    revision_counts: dict[str, int] = {}
    fresh_ms: list[int] = []
    all_ms: list[int] = []
    controls: list[int] = []
    for probe in probes:
        hash_counts[probe["hash"]] = hash_counts.get(probe["hash"], 0) + 1
        revision_counts[probe["revision"]] = revision_counts.get(probe["revision"], 0) + 1
        ms = int(probe["ms"])
        all_ms.append(ms)
        if probe.get("cache") != "hit":
            fresh_ms.append(ms)
        controls.append(int(probe["controls"]))

    unique_hashes = len(hash_counts)
    print(f"probes: {total} (injected={injected}, dedupe-skipped={total - injected})")
    if any(p.get("cache") for p in probes):
        print(
            f"H1 cache: hits={hits}/{total} ({100 * hits / total:.0f}%), "
            f"fresh={fresh}, errors={errors}"
        )
    print(f"H1 result_hash repetition: {unique_hashes} unique across {total} probes")
    top = sorted(hash_counts.items(), key=lambda kv: -kv[1])[:5]
    for digest, count in top:
        print(f"  {digest}: x{count}")
    print(f"distinct revisions: {len(revision_counts)}")

    def dist(values: list[int], label: str) -> None:
        if not values:
            return
        print(
            f"H2 {label}: n={len(values)} min={min(values)} "
            f"p50={percentile(values, 0.5)} p95={percentile(values, 0.95)} "
            f"max={max(values)} mean={statistics.mean(values):.0f}ms"
        )

    dist(fresh_ms, "inspection_ms (fresh scans)")
    if fresh_ms and all_ms != fresh_ms:
        dist(all_ms, "inspection_ms (all turns incl. cached objects)")
    if controls:
        print(f"controls_scanned: min={min(controls)} max={max(controls)}")


if __name__ == "__main__":
    main()
