from __future__ import annotations

from typing import Protocol


class HumanChannel(Protocol):
    async def send_artifact(
        self,
        *,
        path: str,
        caption: str,
    ) -> None: ...

    async def ask(
        self,
        *,
        question: str,
        context: str = "",
        url: str = "",
        company: str = "System",
        role: str = "Application",
        options: list[str] | None = None,
        risk: str = "medium",
        image_path: str = "",
    ) -> str: ...

    async def confirm(
        self,
        *,
        question: str,
        context: str = "",
        url: str = "",
        company: str = "System",
        role: str = "Application",
    ) -> bool: ...
