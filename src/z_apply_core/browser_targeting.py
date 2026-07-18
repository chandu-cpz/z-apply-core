from __future__ import annotations

from playwright.async_api import Locator, Page

AUTH_INPUT_SELECTOR = (
    'input[type="email"], input[type="password"], input[autocomplete="username"], '
    'input[autocomplete="email"], input[autocomplete="current-password"], '
    'input[autocomplete="new-password"], input[autocomplete="one-time-code"]'
)
STRONG_AUTH_INPUT_SELECTOR = (
    'input[type="password"], input[autocomplete="current-password"], '
    'input[autocomplete="new-password"], input[autocomplete="one-time-code"]'
)
NATIVE_SUBMIT_XPATH = (
    "xpath=ancestor-or-self::*[self::button or "
    "(self::input and (@type='submit' or @type='image'))][1]"
)


async def resolve_file_input(page: Page, target: Locator) -> Locator | None:
    direct = target.and_(page.locator('input[type="file"]'))
    if await direct.count() == 1:
        return direct

    scopes = target.locator("xpath=ancestor-or-self::*")
    for index in range(await scopes.count()):
        scope = scopes.nth(index)
        controlled = await _controlled_file_input(page, scope)
        if controlled is not None:
            return controlled
        candidates = scope.locator('input[type="file"]')
        if await candidates.count() == 1:
            return candidates.first
    return None


async def is_direct_file_upload_trigger(page: Page, target: Locator) -> bool:
    if await target.and_(page.locator('input[type="file"]')).count() == 1:
        return True
    label = target.locator("xpath=ancestor-or-self::label[1]")
    if await label.count() == 1:
        if await label.locator('input[type="file"]').count() == 1:
            return True
        if await _controlled_file_input(page, label) is not None:
            return True
    return await _controlled_file_input(page, target) is not None


async def classify_submit_control(page: Page, target: Locator) -> tuple[str, Locator | None]:
    control = target.locator(NATIVE_SUBMIT_XPATH)
    is_proxy = False
    if await control.count() != 1:
        control = await _form_submit_proxy(page, target)
        if control is None:
            return "not_submit", None
        is_proxy = True

    tag = await _tag_hint(control)
    control_type = (await control.get_attribute("type") or "").lower()
    form = await _owning_form(page, control)
    if tag == "button" and control_type not in {"", "submit"} and not is_proxy:
        return "not_submit", None
    if tag == "button" and not control_type and form is None:
        return "not_submit", None
    if tag == "input" and control_type not in {"submit", "image"}:
        return "not_submit", None

    if form is not None and (
        await form.get_attribute("role") == "search"
        or await form.locator('input[type="search"]').count() > 0
    ):
        return "reversible_search", control
    return "form_submit", control


async def _form_submit_proxy(page: Page, target: Locator) -> Locator | None:
    """Recognize a final JS form action without relying on its visible text."""
    control = target.locator("xpath=ancestor-or-self::button[1]")
    if await control.count() != 1:
        return None
    if (await control.get_attribute("type") or "").lower() != "button":
        return None

    form = await _owning_form(page, control)
    if form is None:
        return None
    if not await form.locator(
        "button[type='submit'], input[type='submit'], input[type='image']"
    ).count():
        return None
    if not await form.locator(
        "input[required], select[required], textarea[required], [aria-required='true']"
    ).count():
        return None

    visible_actions: list[Locator] = []
    buttons = form.locator("button")
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        if await button.is_visible() and not await button.is_disabled():
            visible_actions.append(button)
    if not visible_actions or await control.and_(visible_actions[-1]).count() != 1:
        return None
    return control


async def resolve_auth_submit_control(page: Page, target: Locator) -> Locator | None:
    _kind, native = await classify_submit_control(page, target)
    control = native
    if control is None:
        role_button = target.locator("xpath=ancestor-or-self::*[@role='button'][1]")
        if await role_button.count() != 1:
            return None
        control = role_button

    form = await _owning_form(page, control)
    if form is not None:
        return control if await form.locator(AUTH_INPUT_SELECTOR).count() else None

    scopes = control.locator("xpath=ancestor::*")
    for index in range(await scopes.count()):
        if await scopes.nth(index).locator(STRONG_AUTH_INPUT_SELECTOR).count():
            return control
    return None


async def _controlled_file_input(page: Page, locator: Locator) -> Locator | None:
    values: list[str] = []
    for attribute in ("for", "aria-controls"):
        raw = await locator.get_attribute(attribute)
        if raw:
            values.extend(raw.split())
    for value in values:
        controlled = page.locator(f"xpath=//*[@id={_xpath_literal(value)}]")
        if (
            await controlled.count() == 1
            and (await controlled.get_attribute("type") or "").lower() == "file"
        ):
            return controlled
    return None


async def _owning_form(page: Page, control: Locator) -> Locator | None:
    ancestor = control.locator("xpath=ancestor::form[1]")
    if await ancestor.count() == 1:
        return ancestor
    form_id = await control.get_attribute("form")
    if not form_id:
        return None
    external = page.locator(f"xpath=//form[@id={_xpath_literal(form_id)}]")
    return external if await external.count() == 1 else None


async def _tag_hint(locator: Locator) -> str:
    for tag in ("button", "input"):
        if await locator.and_(locator.page.locator(tag)).count() == 1:
            return tag
    return ""


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"
