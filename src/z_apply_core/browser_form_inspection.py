from __future__ import annotations

from playwright.async_api import Locator, Page

from z_apply_core.browser_observation import BrowserCapabilities, BrowserControlState
from z_apply_core.browser_readiness import (
    BrowserFormReadiness,
    FormControlBlocker,
    SubmitControlState,
)

CONTROL_SELECTOR = (
    'input:not([type="hidden"]), select, textarea, [contenteditable="true"], [role="combobox"]'
)
SUBMIT_SELECTOR = (
    'button[type="submit"], input[type="submit"], input[type="image"], form button:not([type])'
)
ACTION_SELECTOR = (
    'button, a[href], input, select, textarea, [role="button"], [role="link"], [role="combobox"]'
)
STRONG_AUTH_INPUT_SELECTOR = (
    'input[type="password"], input[autocomplete="current-password"], '
    'input[autocomplete="new-password"], input[autocomplete="one-time-code"]'
)


async def inspect_page_capabilities(page: Page) -> BrowserCapabilities:
    controls = await _visible_enabled(page.locator(CONTROL_SELECTOR))
    unresolved = 0
    invalid = 0
    for control in controls:
        if await _is_required(control) and not await _has_value(control):
            unresolved += 1
        if await _is_invalid(page, control):
            invalid += 1

    auth_gate = bool(await _visible_enabled(page.locator(STRONG_AUTH_INPUT_SELECTOR)))
    file_inputs = await _visible_enabled(page.locator('input[type="file"]'))
    empty_file_inputs = [control for control in file_inputs if not await _has_value(control)]
    required_upload = False
    for control in empty_file_inputs:
        if await _is_required(control):
            required_upload = True
            break

    enabled_submit = 0
    disabled_submit = 0
    for control in await _visible(page.locator(SUBMIT_SELECTOR)):
        if await _is_disabled(control):
            disabled_submit += 1
        else:
            enabled_submit += 1

    actions = await _visible_enabled(page.locator(ACTION_SELECTOR))
    visual_only = not actions and await _large_visual_surface_visible(page)
    return BrowserCapabilities(
        editable_controls_visible=bool(controls),
        unresolved_required_controls=unresolved,
        invalid_controls=invalid,
        auth_gate_visible=auth_gate,
        empty_file_upload_present=bool(empty_file_inputs),
        required_file_upload_pending=required_upload,
        enabled_form_submit_visible=enabled_submit > 0,
        disabled_form_submit_visible=disabled_submit > 0,
        visual_only_surface_visible=visual_only,
    )


async def inspect_page_readiness(page: Page) -> BrowserFormReadiness:
    blockers: list[FormControlBlocker] = []
    for control in await _visible_enabled(page.locator(CONTROL_SELECTOR)):
        reasons: list[str] = []
        if await _is_required(control) and not await _has_value(control):
            reasons.append("required control is empty")
        if await control.get_attribute("aria-invalid") == "true":
            reasons.append("control is marked aria-invalid")
        elif await _is_invalid(page, control):
            reasons.append("native constraint validation failed")
        if reasons:
            reasons.extend(await _described_by_text(page, control))
            blockers.append(
                FormControlBlocker(
                    control=await _control_name(control),
                    reasons=tuple(dict.fromkeys(reasons)),
                )
            )

    submit_controls: list[SubmitControlState] = []
    for control in await _visible(page.locator(SUBMIT_SELECTOR)):
        submit_controls.append(
            SubmitControlState(
                control=await _control_name(control),
                disabled=await _is_disabled(control),
            )
        )
    return BrowserFormReadiness(
        blockers=tuple(blockers),
        submit_controls=tuple(submit_controls),
    )


async def inspect_control(page: Page, locator: Locator, target: str) -> BrowserControlState:
    value = await _control_value(locator)
    return BrowserControlState(
        target=target,
        value=value,
        has_value=await _has_value(locator),
        required=await _is_required(locator),
        invalid=await _is_invalid(page, locator),
        disabled=await _is_disabled(locator),
    )


async def required_file_upload_pending(page: Page) -> bool:
    for control in await _visible_enabled(page.locator('input[type="file"]')):
        if await _is_required(control) and not await _has_value(control):
            return True
    return False


async def _visible(locator: Locator) -> list[Locator]:
    return [
        item
        for item in (locator.nth(index) for index in range(await locator.count()))
        if await item.is_visible()
    ]


async def _visible_enabled(locator: Locator) -> list[Locator]:
    return [item for item in await _visible(locator) if not await _is_disabled(item)]


async def _is_disabled(locator: Locator) -> bool:
    return await locator.is_disabled() or await locator.get_attribute("aria-disabled") == "true"


async def _is_required(locator: Locator) -> bool:
    return (
        await locator.get_attribute("required") is not None
        or await locator.get_attribute("aria-required") == "true"
    )


async def _is_invalid(page: Page, locator: Locator) -> bool:
    return (
        await locator.get_attribute("aria-invalid") == "true"
        or await locator.and_(page.locator(":invalid")).count() == 1
    )


async def _has_value(locator: Locator) -> bool:
    control_type = (await locator.get_attribute("type") or "").lower()
    if control_type in {"checkbox", "radio"}:
        return await locator.is_checked()
    return bool((await _control_value(locator)).strip())


async def _control_value(locator: Locator) -> str:
    try:
        return (await locator.input_value()).strip()
    except Exception:
        return (await locator.text_content() or "").strip()


async def _control_name(locator: Locator) -> str:
    for attribute in ("aria-label", "name", "placeholder", "id"):
        value = await locator.get_attribute(attribute)
        if value and value.strip():
            return value.strip()
    return "unnamed control"


async def _described_by_text(page: Page, locator: Locator) -> list[str]:
    ids = (await locator.get_attribute("aria-describedby") or "").split()
    descriptions: list[str] = []
    for value in ids:
        description = page.locator(f"xpath=//*[@id={_xpath_literal(value)}]")
        if await description.count() != 1 or not await description.is_visible():
            continue
        text = (await description.text_content() or "").strip()
        if text:
            descriptions.append(text)
    return descriptions


async def _large_visual_surface_visible(page: Page) -> bool:
    viewport = page.viewport_size
    width = viewport["width"] if viewport is not None else 0
    height = viewport["height"] if viewport is not None else 0
    minimum_area = max(40_000, width * height * 0.2)
    for surface in await _visible(page.locator("canvas, video, iframe, img")):
        box = await surface.bounding_box()
        if box and box["width"] * box["height"] >= minimum_area:
            return True
    return False


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"
