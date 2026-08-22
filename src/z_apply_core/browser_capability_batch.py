"""Single-roundtrip capability inspection.

The legacy per-control path in :mod:`z_apply_core.browser_form_inspection`
spends 4-6 sequential playwright RPCs per matched control; a 30-60-control
form pays ~150-400 roundtrips on every model call. This module collects the
same raw facts for every selector set in ONE ``page.evaluate`` pass and
rebuilds :class:`BrowserCapabilities` from the records with pure-Python
mirrors of the legacy decision trees.

Failure contract (binding acceptance criterion): the batch evaluate runs
under an explicit timeout; on timeout or JS exception the caller degrades to
the legacy path for that turn instead of failing it.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Any

from playwright.async_api import Page

from z_apply_core.browser_form_inspection import (
    ACTION_SELECTOR,
    CONTROL_SELECTOR,
    MAX_UNRESOLVED_NAMES,
    STRONG_AUTH_INPUT_SELECTOR,
    SUBMIT_SELECTOR,
    inspect_page_capabilities,
)
from z_apply_core.browser_observation import BrowserCapabilities

logger = logging.getLogger(__name__)

DEFAULT_BATCH_TIMEOUT_S = 2.0

# One DOM pass returning raw per-element records. The Python side owns every
# decision tree (see capabilities_from_records / _classify_submit_record); the
# JS only measures facts, so parity risk stays in one reviewable place.
_BATCH_JS = """
(selectors) => {
  const visible = (el) => {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rects = el.getClientRects();
    if (!rects.length) return false;
    return rects[0].width > 0 || rects[0].height > 0;
  };
  const enabled = (el) =>
    !(el.disabled === true || el.getAttribute('aria-disabled') === 'true');
  const firstAttr = (el, names) => {
    for (const name of names) {
      const value = el.getAttribute(name);
      if (value && value.trim()) return value.trim();
    }
    return null;
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit && explicit.trim()) return explicit.trim();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['text', 'email', 'tel', 'number', 'password'].includes(type)) return 'textbox';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    const tag = el.tagName.toLowerCase();
    return tag === 'select' ? 'combobox' : tag;
  };

  const radioGroupsChecked = new Set(
    [...document.querySelectorAll('input[type="radio"]:checked')].map(
      (el) => el.getAttribute('name') || '',
    ),
  );

  const controls = [...document.querySelectorAll(selectors.control)].map((el) => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    let hasValue;
    if (type === 'checkbox' || type === 'radio') {
      hasValue = el.checked === true;
      if (!hasValue && type === 'radio') {
        const name = el.getAttribute('name');
        hasValue = name !== null && radioGroupsChecked.has(name);
      }
    } else if (el.isContentEditable) {
      hasValue = Boolean((el.textContent || '').trim());
    } else {
      hasValue = Boolean(String(el.value ?? '').trim());
    }
    return {
      type,
      required:
        el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
      hasValue,
      invalid:
        el.getAttribute('aria-invalid') === 'true' ||
        (() => { try { return el.matches(':invalid'); } catch { return false; } })(),
      name: firstAttr(el, ['aria-label', 'name', 'placeholder', 'id']),
      visible: visible(el),
      disabled: !enabled(el),
    };
  });

  const formFacts = new Map();
  const formFactsOf = (native) => {
    let form = native.closest('form');
    if (!form) {
      const externalId = native.getAttribute('form');
      if (externalId) {
        const candidate = document.getElementById(externalId);
        if (candidate && candidate.tagName === 'FORM') form = candidate;
      }
    }
    if (!form) return null;
    if (!formFacts.has(form)) {
      formFacts.set(form, {
        isSearch:
          form.getAttribute('role') === 'search' ||
          Boolean(form.querySelector('input[type="search"]')),
      });
    }
    return formFacts.get(form);
  };

  // Legacy semantics: only VISIBLE targets are ever classified, and the
  // native control resolved by ancestor-or-self is what gets its tag/type/
  // disabled state read.
  const submits = [...document.querySelectorAll(selectors.submit)]
    .filter((el) => visible(el))
    .map((el) => {
      const native =
        el.closest('button, input[type="submit"], input[type="image"]') || el;
      const tag = native.tagName.toLowerCase();
      const facts = formFactsOf(native);
      return {
        tag: tag === 'button' || tag === 'input' ? tag : '',
        type: (native.getAttribute('type') || '').toLowerCase(),
        disabled: !enabled(native),
        formPresent: facts !== null,
        formIsSearch: facts ? facts.isSearch : false,
      };
    });

  const actionsVisibleEnabled = [
    ...document.querySelectorAll(selectors.action),
  ].some((el) => visible(el) && enabled(el));

  const authGateVisible = [
    ...document.querySelectorAll(selectors.auth),
  ].some((el) => visible(el) && enabled(el));

  const files = [...document.querySelectorAll('input[type="file"]')]
    .filter((el) => enabled(el))
    .map((el) => ({
      required:
        el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
      empty: !el.value,
    }));

  const viewportMinArea = Math.max(
    40000,
    (window.innerWidth || 0) * (window.innerHeight || 0) * 0.2,
  );
  const bigSurface = [
    ...document.querySelectorAll('canvas, video, iframe, img'),
  ].some((el) => {
    if (!visible(el)) return false;
    const box = el.getBoundingClientRect();
    return box.width * box.height >= viewportMinArea;
  });

  return {
    controls,
    submits,
    actionsVisibleEnabled,
    authGateVisible,
    files,
    bigSurface,
  };
}
"""

_BATCH_SELECTORS = {
    "control": CONTROL_SELECTOR,
    "submit": SUBMIT_SELECTOR,
    "action": ACTION_SELECTOR,
    "auth": STRONG_AUTH_INPUT_SELECTOR,
}


def _classify_submit_record(record: dict[str, Any]) -> str | None:
    """Pure-Python mirror of ``classify_submit_control``'s decision tree.

    Operates on one batch record instead of live locators. Returns
    "form_submit", "reversible_search", or None (= not_submit).
    """
    tag = str(record.get("tag") or "")
    control_type = str(record.get("type") or "")
    form_present = bool(record.get("formPresent"))
    if tag == "button" and control_type not in {"", "submit"}:
        return None
    if tag == "button" and not control_type and not form_present:
        return None
    if tag == "input" and control_type not in {"submit", "image"}:
        return None
    if form_present and record.get("formIsSearch"):
        return "reversible_search"
    return "form_submit"


def capabilities_from_records(
    records: dict[str, Any],
    *,
    started_monotonic: float | None = None,
) -> BrowserCapabilities:
    """Rebuild :class:`BrowserCapabilities` from one batch's records."""
    started = started_monotonic if started_monotonic is not None else _time.monotonic()
    controls = [c for c in records["controls"] if c["visible"] and not c["disabled"]]
    unresolved = 0
    unresolved_names: list[str] = []
    invalid = 0
    for control in controls:
        if control["required"] and not control["hasValue"]:
            unresolved += 1
            if len(unresolved_names) < MAX_UNRESOLVED_NAMES and control["name"]:
                unresolved_names.append(control["name"])
        if control["invalid"]:
            invalid += 1

    required_upload = any(f["required"] and f["empty"] for f in records["files"])

    enabled_submit = 0
    disabled_submit = 0
    for record in records["submits"]:
        kind = _classify_submit_record(record)
        if kind != "form_submit":
            continue
        if record["disabled"]:
            disabled_submit += 1
        else:
            enabled_submit += 1

    visual_only = not records["actionsVisibleEnabled"] and records["bigSurface"]
    return BrowserCapabilities(
        editable_controls_visible=bool(controls),
        unresolved_required_controls=unresolved,
        unresolved_names=tuple(unresolved_names),
        invalid_controls=invalid,
        auth_gate_visible=bool(records["authGateVisible"]),
        empty_file_upload_present=any(f["empty"] for f in records["files"]),
        required_file_upload_pending=required_upload,
        enabled_form_submit_visible=enabled_submit > 0,
        disabled_form_submit_visible=disabled_submit > 0,
        visual_only_surface_visible=visual_only,
        inspection_ms=int((_time.monotonic() - started) * 1000),
        controls_scanned=len(controls),
    )


class BatchDegraded(RuntimeError):
    """The batch evaluate failed; caller must fall back to the legacy path."""


async def inspect_page_capabilities_batched(
    page: Page,
    *,
    timeout_s: float = DEFAULT_BATCH_TIMEOUT_S,
) -> BrowserCapabilities:
    """One-evaluate capability scan. Raises :class:`BatchDegraded` on failure."""
    started = _time.monotonic()
    try:
        records = await asyncio.wait_for(
            page.evaluate(_BATCH_JS, _BATCH_SELECTORS), timeout=timeout_s
        )
    except TimeoutError as exc:
        raise BatchDegraded(f"batch evaluate exceeded {timeout_s}s") from exc
    except Exception as exc:  # noqa: BLE001 - any JS/page failure degrades
        raise BatchDegraded(f"batch evaluate failed: {exc}") from exc
    return capabilities_from_records(records, started_monotonic=started)


async def inspect_page_capabilities_with_fallback(
    page: Page,
    *,
    timeout_s: float = DEFAULT_BATCH_TIMEOUT_S,
) -> BrowserCapabilities:
    """Batch-first inspection that degrades to the legacy path, never fails.

    Binding acceptance criterion: a hanging or
    failing batch must log once and degrade — it must never take the turn
    down with it.
    """
    try:
        return await inspect_page_capabilities_batched(page, timeout_s=timeout_s)
    except BatchDegraded as exc:
        logger.warning("capability batch degraded to legacy scan (%s)", exc)
        return await inspect_page_capabilities(page)
