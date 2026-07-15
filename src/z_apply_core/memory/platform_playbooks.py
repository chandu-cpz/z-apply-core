from __future__ import annotations

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
_MAX_LESSON_CHARS = 500


class PlatformPlaybooks:
    """Durable, evidence-gated operational knowledge grouped by site family."""

    def __init__(self, root: Path = DEFAULT_PLAYBOOK_ROOT) -> None:
        self._root = root
        self._lock = Lock()

    def read_for_url(self, url: str) -> str:
        path = self.path_for_url(url)
        if not path.is_file():
            return "No verified platform lessons have been recorded yet."
        content = path.read_text(encoding="utf-8")
        if len(content) <= _MAX_ACTIVE_PLAYBOOK_CHARS:
            return content
        return "# Earlier lessons omitted\n\n" + content[-_MAX_ACTIVE_PLAYBOOK_CHARS:]

    def remember(self, *, job_url: str, lesson: str, receipt: ActionReceipt | None) -> str:
        normalized = " ".join(lesson.split())
        if receipt is None or not receipt.changed:
            raise ToolException(
                "A platform lesson requires the latest typed browser action receipt "
                "to prove a successful state change."
            )
        if not normalized or len(normalized) > _MAX_LESSON_CHARS:
            raise ToolException(
                f"Platform lesson must be 1-{_MAX_LESSON_CHARS} characters."
            )
        if _contains_private_or_ephemeral_data(normalized):
            raise ToolException(
                "Platform lessons cannot contain secrets, candidate facts, file paths, "
                "or ephemeral browser references."
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
        entry = (
            f"\n## {datetime.now(UTC).isoformat(timespec='seconds')}\n\n"
            f"- Lesson: {normalized}\n"
            f"- Verified evidence: {evidence}.\n"
        )
        with self._lock:
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            if f"- Lesson: {normalized}\n" in existing:
                return "Platform lesson was already known."
            if not existing:
                existing = (
                    f"# {expected_family} operational playbook\n\n"
                    "Historical hints only. Current ARIA/DOM evidence always wins. "
                    "Never reuse browser refs, field values, or submission state.\n"
                )
            path.write_text(existing.rstrip() + "\n" + entry, encoding="utf-8")
        return f"Stored one verified lesson in {path.name}."

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
    async def remember_platform_lesson(lesson: str) -> str:
        """Persist one reusable platform lesson proved by the latest successful action.

        Use sparingly after discovering a durable interaction pattern that can make a
        later application faster. Never store refs, values, secrets, paths, or candidate
        facts. Current browser evidence always overrides historical lessons.
        """
        return playbooks.remember(
            job_url=job_url,
            lesson=lesson,
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
