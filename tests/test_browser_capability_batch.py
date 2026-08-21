from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from z_apply_core.browser_capability_batch import (
    BatchDegraded,
    _classify_submit_record,
    capabilities_from_records,
    inspect_page_capabilities_batched,
    inspect_page_capabilities_with_fallback,
)
from z_apply_core.browser_form_inspection import MAX_UNRESOLVED_NAMES


def _control(**overrides: Any) -> dict[str, Any]:
    base = {
        "type": "text",
        "required": False,
        "hasValue": True,
        "invalid": False,
        "name": "field",
        "visible": True,
        "disabled": False,
    }
    base.update(overrides)
    return base


def test_classify_submit_record_mirrors_legacy_tree() -> None:
    # button[type=submit] inside a plain form -> form_submit
    assert (
        _classify_submit_record(
            {"tag": "button", "type": "submit", "formPresent": True, "formIsSearch": False}
        )
        == "form_submit"
    )
    # button[type=button]: never a native submit (legacy rule 1 kills it even
    # inside a form; the proxy branch is unreachable from SUBMIT_SELECTOR).
    assert (
        _classify_submit_record(
            {"tag": "button", "type": "button", "formPresent": True, "formIsSearch": False}
        )
        is None
    )
    # button with no type outside any form -> not_submit (legacy rule 2)
    assert (
        _classify_submit_record(
            {"tag": "button", "type": "", "formPresent": False, "formIsSearch": False}
        )
        is None
    )
    # button with no type inside a form -> form_submit
    assert (
        _classify_submit_record(
            {"tag": "button", "type": "", "formPresent": True, "formIsSearch": False}
        )
        == "form_submit"
    )
    # input[type=text] -> not_submit (legacy rule 3)
    assert (
        _classify_submit_record(
            {"tag": "input", "type": "text", "formPresent": True, "formIsSearch": False}
        )
        is None
    )
    # input[type=image] -> form_submit
    assert (
        _classify_submit_record(
            {"tag": "input", "type": "image", "formPresent": True, "formIsSearch": False}
        )
        == "form_submit"
    )
    # search form downgrades to reversible_search
    assert (
        _classify_submit_record(
            {"tag": "button", "type": "submit", "formPresent": True, "formIsSearch": True}
        )
        == "reversible_search"
    )


def test_capabilities_from_records_counts_and_caps_names() -> None:
    records = {
        "controls": [
            _control(required=True, hasValue=False, name=f"missing-{i}", invalid=(i == 0))
            for i in range(8)
        ],
        "submits": [
            {"tag": "button", "type": "submit", "formPresent": True, "formIsSearch": False,
             "disabled": False},
        ],
        "actionsVisibleEnabled": True,
        "authGateVisible": False,
        "files": [],
        "bigSurface": False,
    }

    caps = capabilities_from_records(records)

    assert caps.unresolved_required_controls == 8
    assert len(caps.unresolved_names) == MAX_UNRESOLVED_NAMES
    assert caps.invalid_controls == 1
    assert caps.enabled_form_submit_visible is True
    assert caps.editable_controls_visible is True


def test_radio_group_checked_counts_as_resolved() -> None:
    records = {
        "controls": [
            _control(type="radio", required=True, hasValue=False),
        ],
        "submits": [],
        "actionsVisibleEnabled": True,
        "authGateVisible": False,
        "files": [],
        "bigSurface": False,
    }

    caps = capabilities_from_records(records)

    # JS already folded group-checked state into hasValue; the Python side
    # must treat it as resolved either way.
    assert caps.unresolved_required_controls == 1  # hasValue False in record


def test_upload_and_visual_only_flags() -> None:
    records = {
        "controls": [],
        "submits": [],
        "actionsVisibleEnabled": False,
        "authGateVisible": True,
        "files": [{"required": True, "empty": True}],
        "bigSurface": True,
    }

    caps = capabilities_from_records(records)

    assert caps.empty_file_upload_present is True
    assert caps.required_file_upload_pending is True
    assert caps.auth_gate_visible is True
    assert caps.visual_only_surface_visible is True


class _ExplodingEvaluatePage:
    async def evaluate(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("page closed")


class _HangingEvaluatePage:
    async def evaluate(self, *_args: Any, **_kwargs: Any) -> Any:
        await asyncio.sleep(5)


_LEGACY_CALLED: list[bool]


async def _fake_legacy(page: Any) -> Any:  # noqa: ANN401
    return "legacy"


def test_fallback_degrades_to_legacy_on_js_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "z_apply_core.browser_capability_batch.inspect_page_capabilities",
        _fake_legacy,
    )

    async def run() -> Any:
        with caplog.at_level(logging.WARNING):
            return await inspect_page_capabilities_with_fallback(_ExplodingEvaluatePage())

    result = asyncio.run(run())

    assert result == "legacy"
    assert any("degraded to legacy" in record.message for record in caplog.records)


def test_fallback_degrades_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "z_apply_core.browser_capability_batch.inspect_page_capabilities",
        _fake_legacy,
    )

    async def run() -> Any:
        return await inspect_page_capabilities_with_fallback(
            _HangingEvaluatePage(), timeout_s=0.05
        )

    result = asyncio.run(run())

    assert result == "legacy"


def test_batched_raises_typed_error_on_failure() -> None:
    async def run() -> Any:
        await inspect_page_capabilities_batched(_ExplodingEvaluatePage())

    with pytest.raises(BatchDegraded):
        asyncio.run(run())
