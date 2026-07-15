from __future__ import annotations

from functools import lru_cache

from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)

from z_apply_core.agents.prompts import load_prompt


@lru_cache(maxsize=1)
def configure_z_apply_harness_profile() -> None:
    """Remove generic coding-agent delegation guidance from NVIDIA agents."""
    register_harness_profile(
        "NVIDIA",
        HarnessProfile(
            excluded_middleware=frozenset(
                {"TodoListMiddleware", "SummarizationMiddleware"}
            ),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            tool_description_overrides={"task": load_prompt("task_tool.md")},
        ),
    )
