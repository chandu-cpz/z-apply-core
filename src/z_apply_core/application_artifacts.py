from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from z_apply_core.browser_session import BrowserSession
from z_apply_core.human.channel import HumanChannel

logger = logging.getLogger(__name__)


class ApplicationArtifactPublisher:
    """Capture material application states and publish them to the run topic."""

    def __init__(
        self,
        *,
        browser: BrowserSession,
        channel: HumanChannel,
        on_created: Callable[[str, Path], Awaitable[None]] | None = None,
    ) -> None:
        self._browser = browser
        self._channel = channel
        self._on_created = on_created

    @property
    def browser(self) -> BrowserSession:
        return self._browser

    async def publish_review_pdf(self) -> None:
        path = self._browser.artifact_path("application-review.pdf")
        await self._browser.call_tool(
            "browser_pdf",
            {"filename": path.name},
        )
        if self._on_created is not None:
            await self._on_created("review_pdf", path)
        await self._channel.send_artifact(
            path=str(path),
            caption="Application review PDF — inspect before approving final submission.",
        )
        logger.info("Published pre-submit application PDF to the human channel")

    async def publish_submission_screenshot(self) -> None:
        path = self._browser.artifact_path("submission-confirmation.png")
        await self._browser.call_tool_content(
            "browser_take_screenshot",
            {"filename": path.name},
        )
        if self._on_created is not None:
            await self._on_created("submission_confirmation", path)
        await self._channel.send_artifact(
            path=str(path),
            caption="Application submission confirmed in the browser.",
        )
        logger.info("Published submission confirmation screenshot to the human channel")
