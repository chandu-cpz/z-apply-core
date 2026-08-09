from __future__ import annotations

from functools import lru_cache

from deepagents import FilesystemPermission
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)

from z_apply_core.agents.prompts import load_prompt

ARTIFACTS_VIRTUAL_ROOT = "/.z-apply/runs"
CANDIDATE_CONTEXT_VIRTUAL_PATH = "/chandrakanth_v_resume.md"


@lru_cache(maxsize=1)
def configure_z_apply_harness_profile() -> None:
    """Remove generic coding-agent delegation guidance from NVIDIA agents."""
    register_harness_profile(
        "NVIDIA",
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            tool_description_overrides={"task": load_prompt("task_tool.md")},
        ),
    )


def deepagent_filesystem_permissions(run_id: str = "") -> list[FilesystemPermission]:
    """Read-only sandbox shared by the auth agent and the orchestrator agent."""
    artifact_root = (
        f"{ARTIFACTS_VIRTUAL_ROOT}/{run_id}/browser-artifacts" if run_id else ARTIFACTS_VIRTUAL_ROOT
    )
    return [
        FilesystemPermission(
            operations=["read"],
            paths=[artifact_root, f"{artifact_root}/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["read"],
            paths=[CANDIDATE_CONTEXT_VIRTUAL_PATH],
            mode="allow",
        ),
        FilesystemPermission(operations=["read"], paths=["/**"], mode="deny"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]


DEEPAGENT_FILESYSTEM_PERMISSIONS = deepagent_filesystem_permissions()
