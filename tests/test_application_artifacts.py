from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from z_apply_core.application_artifacts import ApplicationArtifactPublisher


@pytest.mark.asyncio
async def test_application_artifacts_capture_before_publishing() -> None:
    browser = Mock()
    browser.artifact_path.side_effect = lambda filename: Path("/tmp/run") / filename
    browser.call_tool = AsyncMock()
    browser.call_tool_content = AsyncMock()
    channel = Mock()
    channel.send_artifact = AsyncMock()
    publisher = ApplicationArtifactPublisher(browser=browser, channel=channel)

    await publisher.publish_review_pdf()
    await publisher.publish_submission_screenshot()

    browser.call_tool.assert_awaited_once_with(
        "browser_pdf", {"filename": "application-review.pdf"}
    )
    browser.call_tool_content.assert_awaited_once_with(
        "browser_take_screenshot", {"filename": "submission-confirmation.png"}
    )
    assert channel.send_artifact.await_count == 2
