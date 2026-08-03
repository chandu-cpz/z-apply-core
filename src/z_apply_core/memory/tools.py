from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool

if TYPE_CHECKING:
    from z_apply_core.memory.applicant_memory import CandidateMemory


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

        sources: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for result in (lookup_result, search_result):
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
            "lookup": lookup_result,
            "search": search_result,
            "sources": sources,
        }

    return [lookup_candidate_memory]
