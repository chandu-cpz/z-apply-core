from __future__ import annotations

from functools import lru_cache
from importlib import resources

ORCHESTRATOR_PROMPT = "orchestrator.md"


@lru_cache
def load_prompt(name: str) -> str:
    """Load a prompt body from the packaged prompts directory."""
    return resources.files(__name__).joinpath(name).read_text(encoding="utf-8")
