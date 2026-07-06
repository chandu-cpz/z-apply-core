from __future__ import annotations

from functools import lru_cache
from importlib import resources


@lru_cache
def load_prompt(name: str) -> str:
    return resources.files(__name__).joinpath(name).read_text(encoding="utf-8")
