from __future__ import annotations

from functools import lru_cache
from importlib import resources


@lru_cache
def _load_tool_call_rules() -> str:
    return resources.files(__name__).joinpath("tool_call_rules.md").read_text(encoding="utf-8")


@lru_cache
def load_prompt(name: str) -> str:
    body = resources.files(__name__).joinpath(name).read_text(encoding="utf-8")
    return f"{_load_tool_call_rules()}\n\n{body}"
