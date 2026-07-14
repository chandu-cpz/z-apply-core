from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from z_apply_core.application_artifacts import ApplicationArtifactPublisher


@pytest.mark.asyncio
async def test_application_artifacts_capture_before_publishing(tmp_path: Path) -> None:
    browser = Mock()
    browser.artifact_path.side_effect = lambda filename: tmp_path / filename
    browser.call_tool = AsyncMock(
        side_effect=lambda _name, arguments: (tmp_path / str(arguments["filename"])).touch()
    )
    channel = Mock()
    channel.send_artifact = AsyncMock()
    publisher = ApplicationArtifactPublisher(browser=browser, channel=channel)

    await publisher.publish_review_artifact()
    await publisher.publish_submission_screenshot()

    assert browser.call_tool.await_args_list[0].args == (
        "browser_take_screenshot",
        {
            "filename": "application-review.png",
            "fullPage": True,
            "type": "png",
            "scale": "css",
        },
    )
    assert browser.call_tool.await_args_list[1].args == (
        "browser_take_screenshot",
        {"filename": "submission-confirmation.png", "type": "png", "scale": "css"},
    )
    assert channel.send_artifact.await_count == 2
