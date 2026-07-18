from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

from langchain_core.tools import BaseTool, ToolException, tool

from z_apply_core.browser_observation import ActionReceipt
from z_apply_core.browser_session import BrowserSession

CORE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PLAYBOOK_ROOT = CORE_ROOT / ".z-apply" / "platform-memory"
_MAX_ACTIVE_PLAYBOOK_CHARS = 6_000
_MAX_EPISODE_PART_CHARS = 400


class PlatformPlaybooks:
    """Durable, evidence-gated operational knowledge grouped by site family."""

    def __init__(self, root: Path = DEFAULT_PLAYBOOK_ROOT) -> None:
        self._root = root
        self._lock = Lock()

    def read_for_url(self, url: str) -> str:
        path = self.path_for_url(url)
        if not path.is_file():
            return "No evidence-backed platform procedures have been recorded yet."
        content = path.read_text(encoding="utf-8")
        if len(content) <= _MAX_ACTIVE_PLAYBOOK_CHARS:
            return content
        return "# Earlier lessons omitted\n\n" + content[-_MAX_ACTIVE_PLAYBOOK_CHARS:]

    def remember(
        self,
        *,
        job_url: str,
        situation: str,
        action: str,
        expected_outcome: str,
        recovery: str,
        receipt: ActionReceipt | None,
    ) -> str:
        episode = {
            "Situation": _normalize_episode_part(situation),
            "Action": _normalize_episode_part(action),
            "Expected outcome": _normalize_episode_part(expected_outcome),
            "Recovery": _normalize_episode_part(recovery),
        }
        if receipt is None or not receipt.changed:
            raise ToolException(
                "A platform episode requires the latest typed browser action receipt "
                "to prove an observed state change."
            )
        for label, value in episode.items():
            if not value or len(value) > _MAX_EPISODE_PART_CHARS:
                raise ToolException(
                    f"{label} must be 1-{_MAX_EPISODE_PART_CHARS} characters."
                )
            if _contains_private_or_ephemeral_data(value):
                raise ToolException(
                    "Platform episodes cannot contain secrets, candidate facts, file "
                    "paths, or ephemeral browser references."
                )
        expected_family = site_family(job_url)
        observed_family = site_family(receipt.after.url)
        if not expected_family or expected_family != observed_family:
            raise ToolException(
                "The latest successful browser action is not on this job's platform."
            )

        path = self.path_for_url(job_url)
        path.parent.mkdir(parents=True, exist_ok=True)
        evidence = (
            f"{receipt.tool} changed browser revision "
            f"{receipt.before_revision} → {receipt.after.revision}"
        )
        procedure_key = hashlib.sha256(
            "\n".join(episode.values()).encode()
        ).hexdigest()[:12]
        evidence_key = hashlib.sha256(
            (
                f"{job_url}\n{receipt.tool}\n{receipt.before_revision}\n"
                f"{receipt.after.revision}\n{receipt.after.signature}"
            ).encode()
        ).hexdigest()[:12]
        heading = f"## Procedure {procedure_key}"
        evidence_line = f"- Browser evidence: {evidence}. <!-- {evidence_key} -->"
        entry = (
            f"\n{heading}\n\n"
            f"- Recorded: {datetime.now(UTC).isoformat(timespec='seconds')}\n"
            + "".join(f"- {label}: {value}\n" for label, value in episode.items())
            + f"{evidence_line}\n"
        )
        with self._lock:
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            if f"<!-- {evidence_key} -->" in existing:
                return "This browser transition was already recorded."
            if heading in existing:
                next_heading = existing.find("\n## ", existing.index(heading) + len(heading))
                insertion = len(existing) if next_heading < 0 else next_heading
                updated = (
                    existing[:insertion].rstrip()
                    + "\n"
                    + evidence_line
                    + "\n"
                    + existing[insertion:].lstrip("\n")
                )
                path.write_text(updated, encoding="utf-8")
                return "Added independent browser evidence to a known procedure."
            if not existing:
                existing = (
                    f"# {expected_family} operational playbook\n\n"
                    "Evidence-backed procedures from earlier runs. Current ARIA/DOM "
                    "evidence always wins. Never reuse browser refs, field values, or "
                    "submission state. Prefer procedures with repeated browser "
                    "evidence.\n"
                )
            path.write_text(existing.rstrip() + "\n" + entry, encoding="utf-8")
        return f"Stored one evidence-backed procedure in {path.name}."

    def path_for_url(self, url: str) -> Path:
        family = site_family(url)
        if not family:
            family = "unknown-site"
        safe_name = "".join(character if character.isalnum() or character in ".-" else "_" for character in family)
        return self._root / f"{safe_name}.md"


def make_platform_memory_tool(
    playbooks: PlatformPlaybooks,
    *,
    job_url: str,
    browser: BrowserSession,
) -> BaseTool:
    @tool
    async def remember_platform_lesson(
        situation: str,
        action: str,
        expected_outcome: str,
        recovery: str,
    ) -> str:
        """Record one reusable procedure bound to the latest changed browser receipt.

        Describe the structural situation, reusable action, expected observable
        outcome, and safe recovery. Never store refs, values, secrets, paths, candidate
        facts, or submission state. Current browser evidence always wins.
        """
        return playbooks.remember(
            job_url=job_url,
            situation=situation,
            action=action,
            expected_outcome=expected_outcome,
            recovery=recovery,
            receipt=browser.last_action_receipt,
        )

    remember_platform_lesson.handle_tool_error = True
    return remember_platform_lesson


def site_family(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    labels = [label for label in hostname.split(".") if label]
    if len(labels) <= 2:
        return hostname
    return ".".join(labels[-2:])


def _contains_private_or_ephemeral_data(lesson: str) -> bool:
    lowered = lesson.lower()
    forbidden = (
        "default_username",
        "default_password",
        "password",
        "secret",
        "token",
        "/home/",
        "ref=",
        "[ref",
        "@",
    )
    return any(marker in lowered for marker in forbidden)


def _normalize_episode_part(value: str) -> str:
    return " ".join(value.split())
