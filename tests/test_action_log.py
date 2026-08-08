from __future__ import annotations

from z_apply_core.browser_observation import ActionReceipt, BrowserObservation
from z_apply_core.context.action_log import ActionLog


def _observation(revision: int) -> BrowserObservation:
    return BrowserObservation.create(
        revision=revision,
        url="https://example.com/jobs/1",
        title="Software Engineer",
        evidence="textbox e1: Name",
    )


def _receipt(tool: str, before_revision: int, changed: bool) -> ActionReceipt:
    return ActionReceipt(
        tool=tool,
        arguments={"target": "e1", "text": "Chandrakanth"},
        before_revision=before_revision,
        after=_observation(before_revision + 1),
        changed=changed,
        result="ok",
    )


def test_record_appends_in_order() -> None:
    log = ActionLog()
    log.record(_receipt("browser_type", 1, True))
    log.record(_receipt("browser_click", 2, True))
    log.record(_receipt("browser_fill_form", 3, False))
    assert len(log) == 3
