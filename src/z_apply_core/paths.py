"""Centralized filesystem paths for z-apply-core.

All ``CORE_ROOT / ".z-apply" / ...`` construction lives here so a layout
change touches one file instead of twelve. ``CORE_ROOT`` is defined here
(base layer: nothing below it imports config) and re-exported by
``config.py`` for backwards compatibility.
"""

from __future__ import annotations

from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[2]


def _workspace_root(start: Path) -> Path:
    """Nearest ancestor containing both package checkouts as siblings.

    Worktrees can nest inside their own repo, so counting levels is not
    enough; the sibling-pair marker is layout-proof.
    """
    for candidate in (start, *start.parents):
        if (candidate / "z-apply-core" / "pyproject.toml").is_file() and (
            candidate / "z-apply-backend" / "pyproject.toml"
        ).is_file():
            return candidate
    raise RuntimeError(
        "z-apply workspace root not found above the running source tree"
    )


# The z-apply workspace root holding the single .env consumed by every
# package (core, backend).
REPO_ROOT = _workspace_root(CORE_ROOT)

# ---------------------------------------------------------------------------
# Root helpers
# ---------------------------------------------------------------------------


def z_apply_root() -> Path:
    """Return ``<repo>/.z-apply``."""
    return CORE_ROOT / ".z-apply"


# ---------------------------------------------------------------------------
# Runs / artifacts
# ---------------------------------------------------------------------------


def runs_root() -> Path:
    """Return ``<repo>/.z-apply/runs``."""
    return z_apply_root() / "runs"


def run_dir(run_id: str) -> Path:
    """Return ``.../runs/<run_id>``."""
    return runs_root() / run_id


def run_artifacts_dir(run_id: str) -> Path:
    """Return ``.../runs/<run_id>/browser-artifacts``."""
    return run_dir(run_id) / "browser-artifacts"


def run_context_dir(run_id: str) -> Path:
    """Return ``.../runs/<run_id>/context``."""
    return run_dir(run_id) / "context"


def captcha_path(run_id: str) -> Path:
    """Return the captcha screenshot path for *run_id*."""
    return run_artifacts_dir(run_id) / "captcha.png"


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def profiles_root() -> Path:
    """Return ``.../.z-apply/profiles``."""
    return z_apply_root() / "profiles"


def master_profile_path() -> Path:
    """Return ``.../.z-apply/browser-profile``."""
    return z_apply_root() / "browser-profile"


# Backwards-compat aliases matching old constant names
PROFILES_ROOT = profiles_root()
DEFAULT_MASTER_PROFILE = master_profile_path()


# ---------------------------------------------------------------------------
# Memory / playbooks
# ---------------------------------------------------------------------------


def qdrant_path() -> Path:
    """Return ``.../.z-apply/qdrant``."""
    return z_apply_root() / "qdrant"


def playbooks_root() -> Path:
    """Return ``.../.z-apply/platform-memory``."""
    return z_apply_root() / "platform-memory"


# ---------------------------------------------------------------------------
# Ledger history
# ---------------------------------------------------------------------------


def history_path() -> Path:
    """Return ``.../.z-apply/llm-ledger.jsonl``."""
    return z_apply_root() / "llm-ledger.jsonl"


# ---------------------------------------------------------------------------
# Input / candidate
# ---------------------------------------------------------------------------


def input_dir() -> Path:
    """Return ``.../.z-apply/input``."""
    return z_apply_root() / "input"


def resume_path() -> Path:
    """Return the default resume path."""
    return (input_dir() / "Chandrakanth-V-Resume.pdf").resolve()


__all__ = [
    "CORE_ROOT",
    "captcha_path",
    "history_path",
    "input_dir",
    "master_profile_path",
    "playbooks_root",
    "profiles_root",
    "qdrant_path",
    "resume_path",
    "run_artifacts_dir",
    "run_context_dir",
    "run_dir",
    "runs_root",
    "z_apply_root",
]
