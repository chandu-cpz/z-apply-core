from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib import resources


@lru_cache
def load_prompt(name: str) -> str:
    """Load a prompt body from the packaged prompts directory."""
    return resources.files(__name__).joinpath(name).read_text(encoding="utf-8")


DEFAULT_ORCHESTRATOR_PROMPT = "orchestrator.md"

# Preserved pre-rewrite artifact, kept on disk for explicit A/B via the eval
# harness (--variants orchestrator-legacy.md) but not offered as a live
# cockpit/eval default: it is 3.5x the size of the compact default and is not
# a production variant.
_ARTIFACT_VARIANTS = frozenset({"orchestrator-legacy.md"})


@lru_cache
def list_prompt_variants() -> tuple[str, ...]:
    """Return orchestrator prompt variant filenames (``orchestrator*.md``)."""
    names = (
        path.name
        for path in resources.files(__name__).iterdir()
        if path.name.startswith("orchestrator") and path.name.endswith(".md")
    )
    variants = sorted(name for name in names if name not in _ARTIFACT_VARIANTS)
    if DEFAULT_ORCHESTRATOR_PROMPT not in variants:
        variants = [DEFAULT_ORCHESTRATOR_PROMPT, *variants]
    return tuple(variants)


def resolve_orchestrator_prompt(variant: str | None) -> str:
    """Resolve a prompt variant name to a loadable prompt filename.

    ``None`` or empty uses the default. Unknown variants fall back to the
    default rather than crashing the run.
    """
    name = (variant or "").strip()
    if not name or name == DEFAULT_ORCHESTRATOR_PROMPT:
        return DEFAULT_ORCHESTRATOR_PROMPT
    if name.startswith("orchestrator") and name.endswith(".md"):
        try:
            resources.files(__name__).joinpath(name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return DEFAULT_ORCHESTRATOR_PROMPT
        return name
    return DEFAULT_ORCHESTRATOR_PROMPT


def prompt_sha(name: str) -> str:
    """sha256 of the resolved prompt text for one variant (tool rules + body)."""
    return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()
