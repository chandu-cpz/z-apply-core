from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BrowserObservation:
    """One revisioned view of the browser-owned accessibility evidence."""

    revision: int
    signature: str
    url: str
    title: str
    evidence: str

    @classmethod
    def create(
        cls,
        *,
        revision: int,
        url: str,
        title: str,
        evidence: str,
    ) -> BrowserObservation:
        signature = hashlib.sha256(
            f"{url}\0{title}\0{evidence}".encode("utf-8", errors="replace")
        ).hexdigest()
        return cls(
            revision=revision,
            signature=signature,
            url=url,
            title=title,
            evidence=evidence,
        )

    def render(self) -> str:
        return (
            "BROWSER OBSERVATION\n"
            f"revision: {self.revision}\n"
            f"signature: {self.signature[:16]}\n"
            f"url: {self.url or '(unknown)'}\n"
            f"title: {self.title or '(untitled)'}\n"
            "current accessibility evidence:\n"
            f"{self.evidence}"
        )


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """Browser-owned evidence describing one completed mutation attempt."""

    tool: str
    arguments: dict[str, Any]
    before_revision: int
    after: BrowserObservation
    changed: bool
    result: str = ""

    def render(self) -> str:
        return (
            "BROWSER ACTION RECEIPT\n"
            f"action: {self.tool}\n"
            f"arguments: {self.arguments}\n"
            f"before_revision: {self.before_revision}\n"
            f"after_revision: {self.after.revision}\n"
            f"changed: {'true' if self.changed else 'false'}\n"
            f"executor_result: {self.result or '(no separate result)'}\n"
            f"{self.after.render()}"
        )
