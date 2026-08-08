from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool

from z_apply_core.memory.applicant_memory import is_sensitive_fact_label

if TYPE_CHECKING:
    from z_apply_core.memory.applicant_memory import CandidateMemory


def _drop_sensitive_matches(result: dict[str, object] | None) -> dict[str, object] | None:
    """Return the lookup/search result with credential-labeled matches removed.

    A password or token that leaked into memory during an earlier run must
    never reach a model through any part of the tool result, including the raw
    ``matches`` lists, not just the merged ``sources``.
    """
    if result is None:
        return None
    matches = result.get("matches", [])
    if not isinstance(matches, list):
        return result
    kept = [
        match
        for match in matches
        if not (
            isinstance(match, dict) and is_sensitive_fact_label(str(match.get("field_label", "")))
        )
    ]
    if len(kept) == len(matches):
        return result
    # Every usable match was a credential: report an internally consistent
    # empty result instead of a misleading "exact"/"semantic" status with no
    # matches, which could otherwise push a weak model into a re-lookup loop.
    return {**result, "memory_status": "empty", "matches": []}


def make_candidate_memory_tools(candidate_memory: CandidateMemory) -> list[BaseTool]:
    """Expose best-effort candidate-memory retrieval to specialists."""

    @tool
    async def lookup_candidate_memory(
        query: str,
        field_label: str = "",
        limit: int = 5,
    ) -> dict[str, object]:
        """Search previously stored candidate facts from earlier runs or human answers.

        Best-effort retrieval: a missing or empty result does not imply the value is
        absent anywhere else. Pass `field_label` when you know the exact field label
        to prefer the exact-label lookup; otherwise give a free-text `query`. Never
        guess a value the tool did not return.
        """
        lookup_result = None
        if field_label:
            lookup_result = await candidate_memory.lookup(
                field_label=field_label,
                question=query or field_label,
                limit=limit,
            )
        search_result = await candidate_memory.search(
            query=query or field_label,
            limit=limit,
        )

        clean_lookup = _drop_sensitive_matches(lookup_result)
        clean_search = _drop_sensitive_matches(search_result)

        sources: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for result in (clean_lookup, clean_search):
            if result is None:
                continue
            matches = result.get("matches", [])
            if not isinstance(matches, list):
                continue
            for match in matches:
                if not isinstance(match, dict):
                    continue
                field_value = str(match.get("field_label", ""))
                answer = str(match.get("answer", ""))
                if (field_value, answer) in seen:
                    continue
                seen.add((field_value, answer))
                sources.append(
                    {
                        "field_label": field_value,
                        "answer": answer,
                        "source": str(match.get("source", "")),
                        "similarity": match.get("similarity"),
                    }
                )

        return {
            "lookup": clean_lookup,
            "search": clean_search,
            "sources": sources,
        }

    return [lookup_candidate_memory]
