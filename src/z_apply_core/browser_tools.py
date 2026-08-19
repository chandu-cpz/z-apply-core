from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from inspect import Parameter
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast, get_origin

from langchain_core.messages import ToolMessage
from langchain_core.tools import (
    BaseTool,
    InjectedToolCallId,
    StructuredTool,
    ToolException,
    tool,
)
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    create_model,
    model_validator,
)

_ELEMENT_REF_CORE = r"(?:f\d+)?e\d+(?:v\d+)?"
ELEMENT_REF_PATTERN = rf"^{_ELEMENT_REF_CORE}$"
REF_TAG_RE = re.compile(r"\[ref=([^\]]+)\]")

TextToolCaller = Callable[[str, dict[str, Any]], Awaitable[str]]
LangChainToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
FileUploader = Callable[[str, list[str], str], Awaitable[str]]
BrowserObserver = Callable[[], Awaitable[str]]
AuthSubmitter = Callable[[str], Awaitable[str]]
VerificationLinkOpener = Callable[[str], Awaitable[str]]
_AGENT_TOOL_DESCRIPTIONS: dict[str, str] = {}
_AGENT_TOOL_DESCRIPTIONS["browser_wait_for"] = (
    "Wait briefly for page state to settle or visible text to appear/disappear. "
    "The time argument is seconds, must be at most 30, and is not milliseconds. "
    "Prefer a visible text condition when one is known."
)

_AGENT_TOOL_DESCRIPTIONS["browser_evaluate"] = (
    "Run a small JavaScript function against the current page. Use it only for a "
    "stubborn form control whose standard writes never land. When you provide a "
    "`target` ref, the resolved element is passed to your function as its FIRST "
    "argument - write `(el) => {...}` and use that argument. Element refs (e.g. "
    "e90) are browser-layer snapshot tokens, NOT DOM attributes; never query "
    "`document.querySelector('[ref=...]')`. On a React or other controlled "
    "input a plain `el.value = value` assignment is ignored by the framework and "
    "leaves the page unchanged. Use the native setter and matching events "
    "instead, e.g.:\n"
    "`const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "
    "'value').set; s.call(el, ''); s.call(el, value); "
    "el.dispatchEvent(new Event('input', {bubbles: true})); "
    "el.dispatchEvent(new Event('change', {bubbles: true}));`\n"
    "Clear the field first through the same native setter, never by assigning "
    "`.value` directly. State the exact target/ref and value you intend, run it, "
    "then verify with fresh browser evidence that the control holds the value. "
    "For an intl-tel-input phone field with a wrong country, set the country "
    "through its API first: `const iti = el.iti || "
    "window.intlTelInputGlobals?.getInstance(el); if (iti) iti.setCountry('in');` "
    "then re-enter the full international number (e.g. +919063812386) through "
    "the native setter and input/change events. "
    "A checkbox/radio/switch is a TOGGLE: prefer a plain `browser_click`, and "
    "never click it twice in a row (a second identical click un-checks it). If "
    "you must use evaluation for a toggle, call a real `el.click()` on the "
    "actual input element - never assign `.checked = true`, which framework "
    "handlers ignore. To READ a control's state that the snapshot cannot show "
    "(custom consent checkbox), use a read-only probe that returns a definitive "
    "value: `(el) => { const i = el.tagName === 'INPUT' ? el : "
    "el.querySelector('input[type=checkbox]'); return i ? i.checked : null; }`. "
    "A result of `null`/`undefined` means there is no native input at all - rely "
    "on the typed context and do not re-click to check. Reading never mutates "
    "and never counts as a write. "
    "If the evaluate receipt reports `changed: false`, the framework ignored the "
    "write - do not repeat it. Never use evaluation to bypass validation, "
    "fabricate a candidate value, or solve a CAPTCHA. "
    "For a stubborn SELECT/COMBOBOX the recipes are: native `<select>` - assign "
    "through the native setter and fire change: `const s = "
    "Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set; "
    "s.call(el, 'VALUE'); el.dispatchEvent(new Event('change', {bubbles: true}));` "
    "(a plain `el.value = ...` is ignored by React). Custom combobox with an "
    "OPEN menu - click the option by exact text: `const opt = "
    "[...document.querySelectorAll('[role=\"option\"]')].find(o => "
    "o.textContent.trim() === 'VALUE'); if (!opt) return false; "
    "opt.dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); opt.click(); "
    "return true;` (react-select selects on mousedown). Searchable combobox - set "
    "the input through the native setter, dispatch a bubbling `input`, and in the "
    "same evaluate click the option only if it is already in the DOM; otherwise "
    "return a follow-up signal so the next turn re-snapshots and clicks the "
    "filtered option."
)

_AGENT_TOOL_DESCRIPTIONS["browser_snapshot"] = (
    "Capture an accessibility snapshot of the current page (ARIA tree with "
    "refs). This is the full-evidence tool: use it whenever you actually need "
    "page structure, option lists, or field values - browser_observe is only a "
    "cheap change probe and returns no page evidence. Prefer mutation receipts "
    "and the injected post-action context first; they already carry a bounded "
    "view. SCOPE IT TO STAY CHEAP: pass `target=<ref>` to snapshot only that "
    "subtree (e.g. the form container's ref, or a specific section) instead of "
    "the whole page, and pass a `depth` (e.g. depth=2) for a shallow tree when "
    "you only need a section or a few controls. Omit both only when you need "
    "the complete page tree."
)

INITIAL_AGENT_BROWSER_TOOLS = (
    "browser_navigate",
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_evaluate",
    "browser_tabs",
    "browser_wait_for",
    "browser_handle_dialog",
    "browser_take_screenshot",
)

AUTH_AGENT_BROWSER_TOOLS = (
    "browser_snapshot",
    "browser_find",
    "browser_take_screenshot",
    "browser_click",
    "browser_type",
    "browser_wait_for",
    "browser_handle_dialog",
    "browser_tabs",
)

AUTHENTICATION_SPECIALIST_BROWSER_TOOLS = (
    "browser_snapshot",
    "browser_find",
    "browser_take_screenshot",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_wait_for",
    "browser_handle_dialog",
    "browser_tabs",
)

BROWSER_CHANGING_TOOL_NAMES = frozenset(
    {
        "browser_navigate",
        "browser_click",
        "browser_click_upload",
        "browser_type",
        "browser_fill_form",
        "browser_select_option",
        "browser_evaluate",
        "browser_file_upload",
        "browser_handle_dialog",
        "browser_batched",
    }
)

# Model-facing tool results that always embed current browser evidence when
# they succeed. ``browser_snapshot`` returns the raw page capture and records
# the observation; ``browser_wait_for`` appends bounded post-wait evidence.
EVIDENCE_RESULT_TOOL_NAMES = frozenset({"browser_snapshot", "browser_wait_for"})

# Mutation tools whose successful results are evidence-backed receipts. The
# result is only evidence-carrying when a receipt was produced for the call
# itself (the session clears the receipt when the post-action snapshot fails).
RECEIPT_RESULT_TOOL_NAMES = frozenset(
    name for name in BROWSER_CHANGING_TOOL_NAMES if name != "browser_click_upload"
)


def normalize_browser_arguments(
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Translate agent-facing ARIA reference notation at the browser boundary.

    A stray ``ref`` key (models copy ``[ref=e12]`` tokens from the snapshot) is
    folded into ``target`` when ``target`` is absent and otherwise dropped, so
    an ARIA-style call like ``browser_click({"ref": "e12"})`` still works.
    """
    normalized = dict(arguments or {})
    normalized["target"] = _normalize_target(
        normalized.get("target") or normalized.get("ref"),
        element=normalized.get("element"),
    )
    if normalized.get("target") is None:
        normalized.pop("target", None)
    normalized.pop("ref", None)

    fields = normalized.get("fields")
    if isinstance(fields, list):
        normalized_fields: list[Any] = []
        for field in fields:
            if not isinstance(field, Mapping):
                normalized_fields.append(field)
                continue
            entry = dict(field)
            entry["target"] = _normalize_target(
                entry.get("target") or entry.get("ref"),
                element=entry.get("name"),
            )
            entry.pop("ref", None)
            normalized_fields.append(entry)
        normalized["fields"] = normalized_fields
    return normalized


def validate_bounded_wait_arguments(
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Reject ambiguous or excessive model-requested browser waits."""
    normalized = normalize_browser_arguments(arguments)
    raw_time = normalized.get("time")
    if raw_time is None or raw_time == "":
        return normalized
    try:
        seconds = float(raw_time)
    except (TypeError, ValueError) as exc:
        raise ToolException(
            "browser_wait_for.time must be a number of seconds between 0 and 30."
        ) from exc
    if seconds < 0 or seconds > 30:
        raise ToolException(
            "browser_wait_for.time uses seconds and must be between 0 and 30; "
            "milliseconds such as 2000 are invalid. Retry with a short seconds value."
        )
    return normalized


def _normalize_target(value: Any, *, element: Any = None) -> Any:
    reference = _canonical_reference(value)
    if reference is None:
        reference = _explicit_reference(value)
    if reference is None:
        reference = _explicit_reference(element)
    return reference if reference is not None else value


def _canonical_reference(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.startswith("[ref=") and candidate.endswith("]"):
        candidate = candidate[5:-1]
    elif candidate.startswith("ref="):
        candidate = candidate[4:]
    elif candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if re.fullmatch(ELEMENT_REF_PATTERN, candidate):
        return candidate
    return None


def _explicit_reference(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    marker = "[ref="
    start = value.find(marker)
    if start < 0:
        return None
    end = value.find("]", start + len(marker))
    if end < 0:
        return None
    return _canonical_reference(value[start : end + 1])


def _decode_json_container(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _provider_compatible_annotation(annotation: Any) -> Any:
    if get_origin(annotation) not in {list, dict}:
        return annotation
    return Annotated[annotation, BeforeValidator(_decode_json_container)]


MAX_BATCH_STEPS = 20

#: One model-facing scripted browser action. Each step is validated by pydantic
#: (discriminated on ``action``, ``extra="forbid"``) before anything executes.
BatchRunner = Callable[[list[dict[str, Any]]], Awaitable[str]]


class _RefTolerantArguments(BaseModel):
    """Tolerate stray ``ref`` keys produced from snapshot evidence tokens.

    Models copy ``[ref=e12]`` tokens from the snapshot into tool arguments as a
    ``ref`` key, even though the browser-layer tools declare ``target``. Before
    the ``extra="forbid"`` validation runs, any ``ref`` is folded into
    ``target`` when ``target`` is absent and otherwise dropped, so a call like
    ``browser_click({"ref": "e12"})`` resolves instead of being rejected as an
    unknown argument.
    """

    @model_validator(mode="before")
    @classmethod
    def _fold_stray_ref_into_target(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        folded = dict(data)
        if "ref" in folded:
            target = folded.get("target")
            if not target:
                folded["target"] = folded["ref"]
            folded.pop("ref", None)
        fields = folded.get("fields")
        if isinstance(fields, list):
            folded_fields: list[Any] = []
            for field in fields:
                if not isinstance(field, dict):
                    folded_fields.append(field)
                    continue
                entry = dict(field)
                if "ref" in entry:
                    if not entry.get("target"):
                        entry["target"] = entry["ref"]
                    entry.pop("ref", None)
                folded_fields.append(entry)
            folded["fields"] = folded_fields
        return folded


class _BatchStep(_RefTolerantArguments):
    """Shared base: each step tolerates a stray ``ref`` key the model copies
    from snapshot evidence and folds it into ``target`` before validation."""

    model_config = ConfigDict(extra="forbid")


class NavigateStep(_BatchStep):
    action: Literal["navigate"]
    url: str


class ClickStep(_BatchStep):
    action: Literal["click"]
    target: str
    element: str = "control"
    double: bool = False
    modifiers: list[str] = []


class TypeStep(_BatchStep):
    action: Literal["type"]
    target: str
    text: str
    submit: bool = False


class FormFieldStep(_BatchStep):
    """A single control in a ``fill_form`` step; matches the standalone
    ``browser_fill_form`` field shape the backend consumes."""

    target: str
    name: str = ""
    type: str = ""
    value: str = ""


class FillFormStep(_BatchStep):
    action: Literal["fill_form"]
    fields: list[FormFieldStep]


class SelectOptionStep(_BatchStep):
    action: Literal["select_option"]
    target: str
    values: list[str]
    element: str = "combobox"


class WaitForStep(_BatchStep):
    action: Literal["wait_for"]
    time: float = 0.0
    text: str = ""
    textGone: str = ""


class HandleDialogStep(_BatchStep):
    action: Literal["handle_dialog"]
    accept: bool
    promptText: str = ""


class EvaluateStep(_BatchStep):
    action: Literal["evaluate"]
    function: str
    target: str = ""


class SnapshotStep(_BatchStep):
    action: Literal["snapshot"]
    target: str = ""
    depth: int | None = None


BatchStep = Annotated[
    NavigateStep
    | ClickStep
    | TypeStep
    | FillFormStep
    | SelectOptionStep
    | WaitForStep
    | HandleDialogStep
    | EvaluateStep
    | SnapshotStep,
    Field(discriminator="action"),
]


class BrowserBatchArgs(BaseModel):
    """A flat, ordered script of up to ``MAX_BATCH_STEPS`` browser actions."""

    model_config = ConfigDict(extra="forbid")
    steps: Annotated[
        list[BatchStep],
        Field(
            min_length=1,
            max_length=MAX_BATCH_STEPS,
            description="Ordered browser actions, each with an `action` field.",
        ),
    ]


_BATCHED_TOOL_DESCRIPTION = (
    "Execute a flat script of browser actions in ONE call: give up to 20 "
    "sequential `steps`, each with an `action`. Steps run in order against the "
    "current page and stop at the first failing step; the result is one line per "
    "step plus fresh post-batch evidence. Prefer this over individual browser_* "
    "calls whenever you can plan 2+ actions ahead. Actions:\n"
    "- navigate: {url} - open a page\n"
    "- click: {target, element='control'} - click a control; never a file input "
    "(use browser_click_upload for uploads)\n"
    "- type: {target, text, submit=false} - type text, replacing existing text\n"
    "- fill_form: {fields: [{target, value, type?}]} - fill several controls at "
    "once\n"
    "- select_option: {target, values} - select combobox values\n"
    "- wait_for: {time<=30 or text or textGone} - brief settle or visible-text wait\n"
    "- handle_dialog: {accept, promptText} - answer a native dialog\n"
    "- evaluate: {function, target=''} - run a JS function; last resort, recipes "
    "below\n"
    "- snapshot: {target='', depth} - capture evidence mid-script (the final "
    "post-batch snapshot is automatic)\n"
    "Targets are element refs from the snapshot (e.g. e12). NEVER include a "
    "final submit click or an upload step: both are guarded and handled by "
    "dedicated tools.\n"
    "evaluate recipes (only for a stubborn form control whose standard writes "
    "never land): when you pass a `target` ref, the resolved element is passed to "
    "your function as its FIRST argument - write `(el) => {...}` and use that "
    "argument. Element refs (e.g. e90) are browser-layer snapshot tokens, NOT "
    "DOM attributes; never query `document.querySelector('[ref=...]')`. On a "
    "React or other controlled input a plain `el.value = value` assignment is "
    "ignored by the framework and leaves the page unchanged. Use the native "
    "setter and matching events instead, e.g.:\n"
    "`const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "
    "'value').set; s.call(el, ''); s.call(el, value); "
    "el.dispatchEvent(new Event('input', {bubbles: true})); "
    "el.dispatchEvent(new Event('change', {bubbles: true}));`\n"
    "Clear the field first through the same native setter, never by assigning "
    "`.value` directly. State the exact target/ref and value you intend, run it, "
    "then verify with fresh evidence that the control holds the value. "
    "For an intl-tel-input phone field with a wrong country, set the country "
    "through its API first: `const iti = el.iti || "
    "window.intlTelInputGlobals?.getInstance(el); if (iti) iti.setCountry('in');` "
    "then re-enter the full international number (e.g. +919063812386) through "
    "the native setter and input/change events. "
    "A checkbox/radio/switch is a TOGGLE: prefer a plain click step, and "
    "never click it twice in a row (a second identical click un-checks it). If "
    "you must use evaluation for a toggle, call a real `el.click()` on the "
    "actual input element - never assign `.checked = true`, which framework "
    "handlers ignore. To READ a control's state that the snapshot cannot show "
    "(custom consent checkbox), use a read-only probe that returns a definitive "
    "value: `(el) => { const i = el.tagName === 'INPUT' ? el : "
    "el.querySelector('input[type=checkbox]'); return i ? i.checked : null; }`. "
    "A result of `null`/`undefined` means there is no native input at all - rely "
    "on the typed context and do not re-click to check. Reading never mutates "
    "and never counts as a write. "
    "If the evaluate receipt reports `changed: false`, the framework ignored the "
    "write - do not repeat it. Never use evaluation to bypass validation, "
    "fabricate a candidate value, or solve a CAPTCHA. "
    "For a stubborn SELECT/COMBOBOX the recipes are: native `<select>` - assign "
    "through the native setter and fire change: `const s = "
    "Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set; "
    "s.call(el, 'VALUE'); el.dispatchEvent(new Event('change', {bubbles: true}));` "
    "(a plain `el.value = ...` is ignored by React). Custom combobox with an "
    "OPEN menu - click the option by exact text: `const opt = "
    "[...document.querySelectorAll('[role=\"option\"]')].find(o => "
    "o.textContent.trim() === 'VALUE'); if (!opt) return false; "
    "opt.dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); opt.click(); "
    "return true;` (react-select selects on mousedown). Searchable combobox - set "
    "the input through the native setter, dispatch a bubbling `input`, and in the "
    "same evaluate click the option only if it is already in the DOM; otherwise "
    "return a follow-up signal so the next turn re-snapshots and clicks the "
    "filtered option."
)


def make_batched_tool(
    runner: BatchRunner,
    *,
    revision_provider: Callable[[], int | None] | None = None,
) -> BaseTool:
    """Build the Core-only scripted browser action tool for the main agent.

    The pydantic ``args_schema`` validates the whole step list before anything
    executes, so a malformed script is rejected in one round trip instead of
    failing mid-way. On success the result is stamped with ``browser_revision``
    so the capability context treats it as evidence-carrying; a stopped batch
    raises a contained ``ToolException`` (error status, no stamp) so sibling
    mutations in the same response are skipped and evidence re-injects.
    """

    async def _browser_batched(
        steps: Any,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str | ToolMessage:
        if not isinstance(steps, list):
            raise ToolException("browser_batched requires a list of action steps.")
        raw_steps = [
            step.model_dump() if hasattr(step, "model_dump") else dict(step)
            for step in steps
        ]
        try:
            result = await runner(raw_steps)
        except ToolException:
            raise
        except Exception as exc:
            # A raw browser-layer failure (for example a label mistarget that the
            # page rejects as invalid CSS) must stay a contained ToolException.
            # The base tool error handler only converts ToolException; letting a
            # raw exception escape would abort the whole parallel tool-response
            # batch (including unrelated sibling calls).
            raise ToolException(
                "Browser batch failed against the current page. Inspect fresh "
                "browser evidence and retry with a corrected step list."
            ) from exc
        revision = revision_provider() if revision_provider is not None else None
        if revision is None:
            return result
        return ToolMessage(
            content=result,
            tool_call_id=tool_call_id,
            name="browser_batched",
            additional_kwargs={"browser_revision": revision},
        )

    _browser_batched.__name__ = "browser_batched"
    return StructuredTool.from_function(
        coroutine=_browser_batched,
        name="browser_batched",
        description=_BATCHED_TOOL_DESCRIPTION,
        args_schema=BrowserBatchArgs,
        infer_schema=False,
        handle_tool_error=True,
    )



def make_click_upload_tool(
    uploader: FileUploader,
    *,
    default_paths: Sequence[str] = (),
    revision_provider: Callable[[], int | None] | None = None,
) -> BaseTool:
    """Build a core-only direct file-input upload operation."""
    configured_paths = list(default_paths)
    if any(not isinstance(path, str) or not path for path in configured_paths):
        raise ValueError("Configured upload paths must be non-empty strings.")

    @tool
    async def browser_click_upload(
        target: str,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
        paths: Annotated[list[str] | None, BeforeValidator(_decode_json_container)] = None,
        element: str = "file upload control",
        name: str = "",
    ) -> str | ToolMessage:
        """Attach the configured resume directly to a file control without a native chooser.

        ``name`` targets a specific hidden file input by its ``name`` OR ``id``
        attribute when the page has several empty upload controls (e.g. an
        easy-resume field plus the form's own resume field). Discover the
        attributes via browser_evaluate on ``input[type=file]``; pass whichever
        identifier the page exposes (Greenhouse inputs are often id-only).
        """
        resolved_paths = paths or configured_paths
        fallback_note = ""
        if len(resolved_paths) == 1 and len(configured_paths) == 1:
            requested = Path(resolved_paths[0])
            configured = Path(configured_paths[0])
            if not requested.is_file():
                resolved_paths = configured_paths
                if requested.name != configured.name:
                    fallback_note = (
                        f"Requested path does not exist, using configured resume: {configured}"
                    )
        if not resolved_paths or any(
            not isinstance(path, str) or not path for path in resolved_paths
        ):
            raise ValueError("browser_click_upload requires at least one non-empty path.")
        normalized_target = normalize_browser_arguments({"target": target, "element": element}).get(
            "target"
        )
        if not isinstance(normalized_target, str) or not normalized_target:
            raise ValueError("browser_click_upload requires a resolvable target.")
        try:
            result = await uploader(normalized_target, resolved_paths, name)
        except ToolException:
            raise
        except Exception as exc:
            # Raw browser-layer failures (for example a label mistarget that the
            # page rejects as invalid CSS) must stay a contained ToolException.
            # The base tool error handler only converts ToolException; letting a
            # raw exception escape would abort the whole parallel tool-response
            # batch (including unrelated sibling calls).
            raise ToolException(
                "File upload failed against the current page. Inspect fresh "
                "browser evidence and call browser_click_upload on the upload "
                "control; the configured resume path is attached automatically."
            ) from exc
        result_text = f"{fallback_note}\n{result}" if fallback_note else result
        revision = revision_provider() if revision_provider is not None else None
        if revision is None:
            return result_text
        return ToolMessage(
            content=result_text,
            tool_call_id=tool_call_id,
            name="browser_click_upload",
            additional_kwargs={"browser_revision": revision},
        )

    browser_click_upload.handle_tool_error = True
    return browser_click_upload


def make_observe_tool(observer: BrowserObserver) -> BaseTool:
    """Build the Core-only cheap browser state probe for the model.

    The tool returns only a compact state signal (revision, change status, URL,
    title) - never the full evidence tree - so casual state checks stop dumping
    the whole page into the conversation history. It deliberately does NOT stamp
    ``browser_revision`` as evidence-carrying: the capability context then
    re-injects the bounded evidence on the next turn instead of skipping it.
    """

    @tool
    async def browser_observe() -> str:
        """Return a compact browser state probe: revision, change status, URL, title.

        This is a cheap probe, NOT an evidence tool: it returns no page evidence,
        only whether the page changed since the last observation and whether your
        current refs are still valid. For the full accessibility tree (option
        lists, field-by-field verification, ambiguous or shadow DOM), call
        browser_snapshot instead.
        """
        return await observer()

    browser_observe.handle_tool_error = True
    return browser_observe


def make_auth_submit_tool(submitter: AuthSubmitter) -> BaseTool:
    """Build the only submit operation available to AuthenticationSpecialist."""

    @tool
    async def browser_auth_submit(
        target: str,
        element: str | None = "authentication form submit control",
    ) -> str:
        """Submit a structurally verified login or verification form.

        The executor rejects controls outside a form containing an email,
        username, password, or one-time-code input. This tool never authorizes
        final job-application submission.
        """
        try:
            normalized_target = normalize_browser_arguments(
                {
                    "target": target,
                    "element": element or "authentication form submit control",
                }
            ).get("target")
            if not isinstance(normalized_target, str) or not normalized_target:
                raise ToolException("browser_auth_submit requires a resolvable target.")
            return await submitter(normalized_target)
        except ToolException:
            raise
        except Exception as exc:
            raise ToolException(
                "Authentication control is no longer current. Inspect fresh browser "
                "evidence and continue from the resulting page; do not replay the stale ref."
            ) from exc

    browser_auth_submit.handle_tool_error = True
    return browser_auth_submit


def make_verification_link_tool(opener: VerificationLinkOpener) -> BaseTool:
    """Build one temporary-tab lifecycle for an email verification link."""

    @tool
    async def browser_verify_link(url: str) -> str:
        """Open an email verification URL in a temporary tab and restore the app tab.

        The executor preserves the original application tab, opens the URL in a new
        tab, captures verification evidence, closes the temporary tab, selects the
        original tab, and returns evidence from both states. Do not use browser_navigate
        or browser_tabs for email verification.
        """
        if not url.startswith(("https://", "http://")):
            raise ToolException("browser_verify_link requires an absolute HTTP(S) URL.")
        try:
            return await opener(url)
        except ToolException:
            raise
        except Exception as exc:
            raise ToolException(
                "Temporary verification tab failed but cleanup was attempted. Inspect "
                "the current application tab before choosing the next action."
            ) from exc

    browser_verify_link.handle_tool_error = True
    return browser_verify_link


class BrowserToolParameter(Protocol):
    name: str
    annotation: object
    default: object
    description: str | None
    hidden: bool


class BrowserToolSpec(Protocol):
    name: str
    title: str | None
    description: str | None
    parameters: Sequence[BrowserToolParameter]


class BrowserToolRegistry:
    def __init__(
        self,
        specs: Sequence[BrowserToolSpec],
        caller: TextToolCaller,
        *,
        langchain_callers: Mapping[str, LangChainToolCaller] | None = None,
        revision_provider: Callable[[], int | None] | None = None,
        receipt_revision_provider: Callable[[], int | None] | None = None,
    ) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._caller = caller
        self._langchain_callers = dict(langchain_callers or {})
        self._revision_provider = revision_provider
        self._receipt_revision_provider = receipt_revision_provider

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if name not in self._specs:
            available = ", ".join(self.names)
            raise ValueError(f"Unknown browser tool {name!r}. Available tools: {available}")
        return await self._caller(name, normalize_browser_arguments(arguments))

    def langchain_tools(self, names: Iterable[str] | None = None) -> list[BaseTool]:
        selected = self.names if names is None else tuple(names)
        return [
            self._to_langchain_tool(self._specs[name]) for name in selected if name in self._specs
        ]

    def _to_langchain_tool(self, spec: BrowserToolSpec) -> BaseTool:
        async def call_tool(
            tool_call_id: Annotated[str, InjectedToolCallId] = "",
            **kwargs: Any,
        ) -> Any:
            caller = self._langchain_callers.get(spec.name, self._caller)
            arguments = {k: v for k, v in kwargs.items() if v is not None and v != ""}
            result = await caller(spec.name, normalize_browser_arguments(arguments))
            if not isinstance(result, str):
                return result
            revision = self._revision_provider() if self._revision_provider is not None else None
            if revision is None:
                return result
            if spec.name in RECEIPT_RESULT_TOOL_NAMES:
                receipt_revision = (
                    self._receipt_revision_provider()
                    if self._receipt_revision_provider is not None
                    else None
                )
                if receipt_revision != revision:
                    return result
            if spec.name not in EVIDENCE_RESULT_TOOL_NAMES | RECEIPT_RESULT_TOOL_NAMES:
                return result
            if spec.name == "browser_snapshot":
                target = str(arguments.get("target") or "").strip().casefold()
                if target and target != "html":
                    # A scoped subtree snapshot (target=<ref>) is a view the
                    # model asked for, not full-page evidence. Leave it
                    # unstamped so the capability context still injects the
                    # bounded full-page view on the next turn instead of
                    # skipping it.
                    return result
            return ToolMessage(
                content=result,
                tool_call_id=tool_call_id,
                name=spec.name,
                additional_kwargs={"browser_revision": revision},
            )

        return StructuredTool.from_function(
            coroutine=call_tool,
            name=spec.name,
            description=(
                _AGENT_TOOL_DESCRIPTIONS.get(spec.name)
                or spec.description
                or spec.title
                or spec.name
            ),
            args_schema=_tool_model(spec),
            infer_schema=False,
            handle_tool_error=True,
        )


def _tool_model(spec: BrowserToolSpec) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    for parameter in spec.parameters:
        if parameter.hidden:
            continue
        default = ... if parameter.default is Parameter.empty else parameter.default
        fields[parameter.name] = (
            _provider_compatible_annotation(parameter.annotation),
            Field(default=default, description=parameter.description),
        )
    model_name = "".join(part.title() for part in spec.name.split("_")) + "Arguments"
    model_factory = cast(Any, create_model)
    return cast(
        type[BaseModel],
        model_factory(
            model_name,
            __base__=_RefTolerantArguments,
            __config__=ConfigDict(extra="forbid"),
            **fields,
        ),
    )
