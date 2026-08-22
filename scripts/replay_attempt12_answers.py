"""Replay helper: seed CandidateMemory with a prior run's resolved
cockpit answers so the user NEVER re-answers those two questions.

Run ONLY after the embedding-provider fix is live (backend relaunched with
EMBEDDINGS_* env): the replay needs a working embed endpoint.

Honest outcome reporting: remember_human_answer returns False when storage
fails (e.g. embeddings endpoint unreachable); the script prints STORED/FAILED
per answer and exits non-zero if any failed.

Usage: uv run python scripts/replay_attempt12_answers.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from z_apply_core.memory.applicant_memory import CandidateMemory

REQUESTS_PATH = Path("/home/chandu/z-apply/.pi/fleet/evidence/attempt12-data/human-requests.json")


async def main() -> int:
    requests = json.loads(REQUESTS_PATH.read_text())
    resolvable = [
        r
        for r in requests
        if r.get("kind") == "question"
        and r.get("status") == "resolved"
        and (r.get("answer") or "").strip()
    ]
    if not resolvable:
        print("no resolved answers found; nothing to replay")
        return 1

    memory = CandidateMemory()
    failures = 0
    for request in resolvable:
        field_label = (request.get("field_label") or "").strip() or (
            request["question"][:80].strip()
        )
        stored = await memory.remember_human_answer(
            field_label=field_label,
            question=request["question"],
            answer=request["answer"],
        )
        if stored:
            print(f"STORED: {field_label!r} -> {request['answer']!r}")
        else:
            failures += 1
            print(
                f"FAILED: {field_label!r} (remember_human_answer returned False; "
                "check EMBEDDINGS_* config and endpoint reachability)"
            )
    if failures:
        print(f"\n{failures} of {len(resolvable)} answers FAILED to store.")
        return 1

    print("\nverify lookups:")
    for request in resolvable:
        result = await memory.lookup(
            field_label=request.get("field_label") or request["question"][:80],
            question=request["question"],
        )
        matches = result.get("matches") or []
        status = "HIT" if matches else "MISS"
        top = matches[0] if matches else {}
        print(
            f"  [{status}] {str(result.get('field_label'))[:50]!r} "
            f"answer={str(top.get('answer'))[:60]!r}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
