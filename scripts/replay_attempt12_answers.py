"""DEC-015 post-landing replay: seed CandidateMemory with attempt-12's resolved
cockpit answers so the user NEVER re-answers those two questions.

Run ONLY after the embedding-provider fix is live (backend relaunched with
EMBEDDINGS_* env): the replay needs a working embed endpoint.

Usage: uv run python scripts/replay_attempt12_answers.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from z_apply_core.memory.applicant_memory import CandidateMemory

REQUESTS_PATH = Path(
    "/home/chandu/z-apply/.pi/fleet/evidence/attempt12-data/human-requests.json"
)


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
    for request in resolvable:
        field_label = (request.get("field_label") or "").strip() or (
            request["question"][:80].strip()
        )
        await memory.remember_human_answer(
            field_label=field_label,
            question=request["question"],
            answer=request["answer"],
        )
        print(f"stored: {field_label!r} -> {request['answer']!r}")

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
