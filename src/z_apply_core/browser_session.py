from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self
from uuid import uuid4

from langchain_core.tools import ToolException
from playwright_python_mcp.mcp import create_connection

from z_apply_core.browser_config import build_browser_config
from z_apply_core.browser_observation import (
    BROWSER_CAPABILITY_SCRIPT,
    ActionReceipt,
    BrowserCapabilities,
    BrowserObservation,
    SubmissionCapability,
)
from z_apply_core.browser_readiness import (
    FORM_READINESS_SCRIPT,
    REQUIRED_FILE_UPLOAD_PENDING_SCRIPT,
    BrowserFormReadiness,
)
from z_apply_core.browser_tools import (
    BROWSER_CHANGING_TOOL_NAMES,
    BrowserToolRegistry,
    normalize_browser_arguments,
    validate_bounded_wait_arguments,
)

INLINE_CAPTURE_TOOLS = frozenset({"browser_snapshot", "browser_take_screenshot"})
CORE_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = CORE_ROOT / ".z-apply" / "runs"
FILE_INPUT_RESOLUTION_SCRIPT = r"""element => {
    const isFileInput = candidate =>
        candidate instanceof HTMLInputElement && candidate.type === 'file';
    if (isFileInput(element)) return element;

    const uniqueFileInput = container => {
        if (!container || !container.querySelectorAll) return null;
        const inputs = [...container.querySelectorAll('input[type="file"]')];
        return inputs.length === 1 ? inputs[0] : null;
    };

    const label = element.closest('label');
    if (label && isFileInput(label.control)) return label.control;

    const controlledIds = [element.getAttribute('for'), element.getAttribute('aria-controls')]
        .filter(Boolean).flatMap(value => value.trim().split(/\s+/));
    for (const id of controlledIds) {
        const controlled = element.ownerDocument.getElementById(id);
        if (isFileInput(controlled)) return controlled;
    }

    let container = element;
    while (container) {
        const input = uniqueFileInput(container);
        if (input) return input;
        container = container.parentElement;
    }

    const rootInput = uniqueFileInput(element.getRootNode());
    if (rootInput) return rootInput;
    return uniqueFileInput(element.ownerDocument);
}"""


class BrowserToolExecutionError(ToolException):
    """A browser backend tool result explicitly marked as an execution error."""


class SubmitControlKind(StrEnum):
    """Browser-owned structural classification for one activated control."""

    NOT_SUBMIT = "not_submit"
    REVERSIBLE_SEARCH = "reversible_search"
    FORM_SUBMIT = "form_submit"


class MutationGate(Protocol):
    def mutation(self) -> AbstractAsyncContextManager[None]: ...


class BrowserLease(Protocol):
    def owns_current_page(self) -> bool: ...

    async def focus(self) -> None: ...

    async def discover_owned_popups(self) -> None: ...


class BrowserSession:
    def __init__(
        self,
        server: Any,
        *,
        run_id: str,
        backend: Any | None = None,
        tools: Sequence[Any] | None = None,
        mutation_gate: MutationGate | None = None,
        owns_backend: bool = True,
    ) -> None:
        self._server = server
        self._backend = backend if backend is not None else server.backend
        self._mutation_gate = mutation_gate
        self._lease: BrowserLease | None = None
        self._owns_backend = owns_backend
        self.run_id = run_id
        self._submission_guard_active = False
        self._submission_capability: SubmissionCapability | None = None
        self._last_snapshot = ""
        self._last_observation: BrowserObservation | None = None
        self._browser_revision = 0
        self._last_mutation_signature = ""
        self._last_mutation_made_progress = True
        self._last_auth_submit_target = ""
        self._last_auth_submit_snapshot = ""
        self._capture_workspace = ARTIFACT_ROOT / run_id / "browser-artifacts"
        self.tools = BrowserToolRegistry(
            tuple(tools if tools is not None else server.backend_pool.tools),
            self.call_tool,
            langchain_callers={
                **{
                    name: self.call_tool_with_inline_snapshot
                    for name in BROWSER_CHANGING_TOOL_NAMES
                    if name != "browser_click_upload"
                },
                "browser_take_screenshot": self.call_tool_content,
                "browser_wait_for": self.call_bounded_wait,
            },
        )

    @classmethod
    async def start(cls, *, run_id: str | None = None) -> Self:
        resolved_run_id = run_id or uuid4().hex
        return cls(
            await create_connection(build_browser_config(resolved_run_id)), run_id=resolved_run_id
        )

    @classmethod
    def from_backend(
        cls,
        backend: Any,
        *,
        tools: Sequence[Any],
        run_id: str,
        mutation_gate: MutationGate,
    ) -> Self:
        return cls(
            None,
            run_id=run_id,
            backend=backend,
            tools=tools,
            mutation_gate=mutation_gate,
            owns_backend=False,
        )

    def bind_lease(self, lease: BrowserLease) -> None:
        self._lease = lease

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        normalized = normalize_browser_arguments(arguments)
        if name == "browser_snapshot" and "target" not in normalized:
            normalized["target"] = "html"
        page_url = ""
        page_title = ""
        async with self._operation_scope():
            guarded_submit = False
            if name == "browser_click" and await self._is_file_upload_trigger(normalized):
                raise BrowserToolExecutionError(
                    "Native file chooser click rejected. Attach the configured file "
                    "atomically with browser_click_upload(target, paths); never click "
                    "a file input or its upload trigger."
                )
            if self._submission_guard_active:
                if name == "browser_click":
                    guarded_submit = (
                        await self._classify_submit_control(normalized)
                        is SubmitControlKind.FORM_SUBMIT
                    )
                elif name == "browser_type" and normalized.get("submit") is True:
                    guarded_submit = True
                if guarded_submit:
                    await self._require_submission_capability_locked(normalized)
            result = await self._backend.call_tool(
                name,
                normalized,
                meta=self._call_meta(name),
            )
            if name in BROWSER_CHANGING_TOOL_NAMES:
                await self._discover_owned_popups()
            if name == "browser_snapshot":
                page_url, page_title = await self._page_identity()
        _raise_for_tool_error(name, result)
        if guarded_submit:
            capability = self._submission_capability
            if capability is not None:
                capability.consumed = True
        text = _text_content(result)
        if name == "browser_snapshot":
            self._last_snapshot = text
            self._record_observation(text, url=page_url, title=page_title)
        return text

    async def call_bounded_wait(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Execute a bounded wait and return fresh inline browser evidence."""
        result = await self.call_tool(name, validate_bounded_wait_arguments(arguments))
        await self.call_tool("browser_snapshot")
        observation = self.current_observation
        if observation is None:
            raise BrowserToolExecutionError(
                "The bounded wait completed but current browser evidence is unavailable."
            )
        return f"{result}\n{observation.compact_render()}"

    async def observe(self) -> str:
        """Return one revisioned browser observation for the active page."""
        await self.call_tool("browser_snapshot")
        observation = self._last_observation
        if observation is None:
            raise BrowserToolExecutionError("The browser did not produce current evidence.")
        return observation.render()

    async def call_tool_content(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        """Return MCP text and image results as LangChain standard content blocks."""
        async with self._operation_scope():
            result = await self._backend.call_tool(
                name,
                normalize_browser_arguments(arguments),
                meta=self._call_meta(name),
            )
        _raise_for_tool_error(name, result)
        return _content_blocks(result)

    async def call_tool_with_inline_snapshot(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Execute a mutation and return current inline evidence when available."""
        normalized = normalize_browser_arguments(arguments)
        signature = json.dumps(
            {"name": name, "arguments": normalized},
            sort_keys=True,
            default=str,
        )
        if signature == self._last_mutation_signature and not self._last_mutation_made_progress:
            raise BrowserToolExecutionError(
                "Duplicate mutation prevented: the identical previous action left the "
                "browser snapshot unchanged. Choose a different action."
            )
        before_observation = self._current_observation()
        mutation = await self.call_tool(name, arguments)
        try:
            evidence = await self.call_tool("browser_snapshot")
        except BrowserToolExecutionError as exc:
            self._last_mutation_signature = signature
            self._last_mutation_made_progress = True
            return f"{mutation}\nPost-action inline snapshot unavailable: {exc}"
        after = self._last_observation or self._record_observation(evidence)
        changed = before_observation.signature != after.signature
        self._last_mutation_signature = signature
        self._last_mutation_made_progress = changed
        return ActionReceipt(
            tool=name,
            arguments=normalized,
            before_revision=before_observation.revision,
            after=after,
            changed=changed,
            result=mutation,
        ).render()

    async def upload_files(self, target: str, paths: list[str]) -> str:
        """Resolve an upload trigger to its file input without opening a chooser."""
        before = self._current_observation()
        async with self._operation_scope():
            tab = await self._backend._ensure_tab()
            resolved = await tab.resolve_target(target=target)
            locator = resolved.locator
            handle = await locator.evaluate_handle(FILE_INPUT_RESOLUTION_SCRIPT)
            file_input = handle.as_element()
            if file_input is None:
                await handle.dispose()
                raise BrowserToolExecutionError(
                    f"Upload target {target!r} could not be associated with exactly one "
                    "file input. Capture fresh evidence and call browser_click_upload on "
                    "the upload control; never click it to open a native chooser."
                )
            try:
                await file_input.set_input_files(paths)
            finally:
                await handle.dispose()
        evidence = await self.call_tool("browser_snapshot")
        after = self._last_observation or self._record_observation(evidence)
        changed = before.signature != after.signature
        return ActionReceipt(
            tool="browser_click_upload",
            arguments={"target": target, "paths": paths},
            before_revision=before.revision,
            after=after,
            changed=changed,
            result="Files attached directly to the resolved upload control.",
        ).render()

    async def _is_file_upload_trigger(self, arguments: dict[str, Any]) -> bool:
        target = arguments.get("target")
        if not isinstance(target, str) or not target:
            return False
        tab = await self._backend._ensure_tab()
        resolved = await tab.resolve_target(target=target)
        handle = await resolved.locator.evaluate_handle(FILE_INPUT_RESOLUTION_SCRIPT)
        try:
            return handle.as_element() is not None
        finally:
            await handle.dispose()

    async def capture_human_challenge(self, target: str) -> Path:
        """Capture one visible challenge into the run-owned artifact directory."""
        if not target.strip():
            raise BrowserToolExecutionError(
                "A current browser target is required to capture a human challenge."
            )
        path = self.artifact_path("captcha.png")
        await self.call_tool(
            "browser_take_screenshot",
            {"target": target, "filename": path.name, "type": "png", "scale": "css"},
        )
        if not path.is_file():
            raise BrowserToolExecutionError(
                "The browser did not create the requested human-challenge artifact."
            )
        return path

    async def inspect_form_readiness(self) -> BrowserFormReadiness:
        """Capture browser-owned constraint state without asking an LLM to infer it."""
        async with self._operation_scope():
            tab = await self._backend._ensure_tab()
            payload = await tab.page.evaluate(FORM_READINESS_SCRIPT)
        return BrowserFormReadiness.from_browser_payload(payload)

    async def required_file_upload_pending(self) -> bool:
        """Report whether the live form owns an empty required file input."""
        async with self._operation_scope():
            tab = await self._backend._ensure_tab()
            return bool(await tab.page.evaluate(REQUIRED_FILE_UPLOAD_PENDING_SCRIPT))

    async def inspect_capabilities(self) -> BrowserCapabilities:
        """Return compositional structural facts about the current browser page."""
        async with self._operation_scope():
            tab = await self._backend._ensure_tab()
            payload = await tab.page.evaluate(BROWSER_CAPABILITY_SCRIPT)
        return BrowserCapabilities.from_browser_payload(payload)

    async def submit_auth_form(self, target: str) -> str:
        """Submit only a form whose live DOM structure proves an auth purpose."""
        if target == getattr(self, "_last_auth_submit_target", "") and getattr(
            self, "_last_snapshot", ""
        ) == getattr(self, "_last_auth_submit_snapshot", ""):
            raise BrowserToolExecutionError(
                "This authentication submit was already executed against the current "
                "page state. Use its post-action evidence; do not repeat it."
            )
        try:
            async with self._operation_scope():
                tab = await self._backend._ensure_tab()
                locator = (await tab.resolve_target(target=target)).locator
                control_handle = await locator.evaluate_handle(
                    """element => {
                    const selector =
                        'button, input[type="submit"], input[type="image"], [role="button"]';
                    const direct = element.closest(selector);
                    const anchor = direct;
                    if (!anchor) return null;
                    const box = anchor.getBoundingClientRect();
                    const x = box.left + box.width / 2;
                    const y = box.top + box.height / 2;
                    const hitControl = anchor.ownerDocument.elementsFromPoint(x, y)
                        .map(candidate => candidate.closest(selector))
                        .find(candidate => candidate !== null);
                    return hitControl || direct;
                }"""
                )
                submit_control = control_handle.as_element()
                if submit_control is None:
                    await control_handle.dispose()
                    raise BrowserToolExecutionError(
                        "Authentication submit rejected: the target does not resolve to "
                        "a submit control."
                    )
                try:
                    is_auth_submit = await submit_control.evaluate(
                """element => {
                const selector =
                    'button, input[type="submit"], input[type="image"], [role="button"]';
                let control = element.closest(selector);
                if (!control) {
                    const clickLayer = element.closest('[role="button"]');
                    if (clickLayer) {
                        const box = clickLayer.getBoundingClientRect();
                        const x = box.left + box.width / 2;
                        const y = box.top + box.height / 2;
                        control = clickLayer.ownerDocument
                            .elementsFromPoint(x, y)
                            .find(candidate => candidate !== clickLayer &&
                                candidate.matches(selector)) || null;
                    }
                }
                const isComponentButton =
                    control instanceof HTMLElement && control.getAttribute('role') === 'button';
                if (!(control instanceof HTMLButtonElement ||
                      control instanceof HTMLInputElement || isComponentButton)) return false;
                if (control instanceof HTMLInputElement &&
                    control.type !== 'submit' && control.type !== 'image') return false;
                if (control instanceof HTMLButtonElement) {
                    const type = control.getAttribute('type');
                    if (type !== 'submit' && !(type === null && control.form)) return false;
                }
                const isAuthInput = input => {
                    const type = (input.getAttribute('type') || 'text').toLowerCase();
                    const autocomplete = (input.getAttribute('autocomplete') || '')
                        .toLowerCase();
                    return type === 'email' || type === 'password' ||
                        ['username', 'email', 'current-password', 'new-password',
                         'one-time-code'].includes(autocomplete);
                };
                const isStrongAuthInput = input => {
                    const type = (input.getAttribute('type') || 'text').toLowerCase();
                    const autocomplete = (input.getAttribute('autocomplete') || '')
                        .toLowerCase();
                    return type === 'password' ||
                        ['current-password', 'new-password', 'one-time-code']
                            .includes(autocomplete);
                };

                const form = control.form || control.closest('form');
                if (form instanceof HTMLFormElement) {
                    return Array.from(form.querySelectorAll('input')).some(isAuthInput);
                }

                // Component frameworks sometimes implement authentication forms
                // without a native HTMLFormElement. Accept only the nearest bounded
                // ancestor that owns a strong password/OTP control; an ordinary job
                // application section containing only email/username cannot qualify.
                let scope = control.parentElement;
                while (scope && scope !== control.ownerDocument.body) {
                    const inputs = Array.from(scope.querySelectorAll('input'));
                    if (inputs.some(isStrongAuthInput)) return true;
                    scope = scope.parentElement;
                }
                return false;
            }"""
                    )
                    if not is_auth_submit:
                        raise BrowserToolExecutionError(
                            "Authentication submit rejected: the target is not a submit "
                            "control in a structurally identifiable login or verification form."
                        )
                    await submit_control.click(trial=True, timeout=15_000)
                    await submit_control.click(timeout=15_000)
                    result = "Authentication submit control clicked."
                finally:
                    await control_handle.dispose()
                await self._discover_owned_popups()
        except BrowserToolExecutionError:
            raise
        except Exception as exc:
            raise BrowserToolExecutionError(
                "Authentication control is stale, loading, or temporarily covered by "
                "another page element. This is recoverable browser actionability state, "
                "not evidence of a CAPTCHA or security challenge. Wait briefly, capture "
                "fresh evidence, and retry the current auth submit once. Executor cause: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        _raise_for_tool_error("browser_click", result)
        try:
            evidence = await self.call_tool("browser_snapshot")
        except BrowserToolExecutionError as exc:
            return f"Authentication form submitted. Post-action snapshot unavailable: {exc}"
        self._last_auth_submit_target = target
        self._last_auth_submit_snapshot = evidence
        return (
            "AUTHENTICATION_FORM_SUBMITTED_ONCE. Do not replay this submit. Continue "
            "from the post-action evidence below.\n" + evidence
        )

    async def open_verification_link(self, url: str) -> str:
        """Resolve an email link in a temporary tab and always restore the app tab."""
        async with self._operation_scope():
            original = await self._backend._ensure_tab()
            context = original.context
            temporary = await context.new_tab()
            verification_evidence = ""
            verification_title = ""
            try:
                await temporary.check_url_and_navigate(url)
                verification_title = await temporary.page.title()
                verification_evidence = await temporary.capture_snapshot(target="html")
            finally:
                if temporary in context.tabs():
                    await temporary.close()
                if original in context.tabs():
                    await context.select_tab(context.tabs().index(original))

        if original not in context.tabs():
            raise BrowserToolExecutionError(
                "The original application tab closed during email verification."
            )
        original_evidence = await original.capture_snapshot(target="html")
        self._last_snapshot = original_evidence
        return (
            "VERIFICATION_TAB_COMPLETED_AND_CLOSED. The original application tab is "
            "selected again.\n"
            f"Verification tab title: {verification_title or '(empty)'}\n"
            f"Verification evidence:\n{verification_evidence}\n"
            f"Original application evidence after restore:\n{original_evidence}"
        )

    def activate_submission_guard(self) -> None:
        """Require a one-use human capability before application form submission."""
        self._submission_guard_active = True
        self._submission_capability = None

    def set_submit_approval(self, approved: bool) -> None:
        """Approve the pending reviewed capability or revoke it."""
        capability = self._submission_capability
        if not approved:
            self._submission_capability = None
            return
        if capability is None:
            raise BrowserToolExecutionError(
                "Submission approval has no pending reviewed browser target."
            )
        capability.approved = True

    async def prepare_submission_review(self, target: str, final_review: str) -> None:
        """Bind a pending approval to the exact current submit control and page."""
        normalized = normalize_browser_arguments({"target": target})
        normalized_target = normalized.get("target")
        if not isinstance(normalized_target, str) or not normalized_target:
            raise BrowserToolExecutionError(
                "Submission review requires the current final submit control ref."
            )
        if (
            await self._classify_submit_control({"target": normalized_target})
            is not SubmitControlKind.FORM_SUBMIT
        ):
            raise BrowserToolExecutionError(
                "Submission review target is not a current form submit control."
            )
        observation = self._current_observation()
        review_digest = hashlib.sha256(
            f"{observation.signature}\0{final_review}".encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()
        self._submission_capability = SubmissionCapability(
            run_id=self.run_id,
            browser_revision=observation.revision,
            page_signature=observation.signature,
            target=normalized_target,
            review_digest=review_digest,
        )

    @property
    def submission_capability(self) -> SubmissionCapability | None:
        return self._submission_capability

    async def _require_submission_capability_locked(
        self,
        arguments: dict[str, Any],
    ) -> None:
        capability = self._submission_capability
        target = arguments.get("target")
        if (
            capability is None
            or not capability.approved
            or capability.consumed
            or target != capability.target
        ):
            raise BrowserToolExecutionError(
                "Final-form submission is locked. Approval must match the exact current "
                "submit control reviewed through request_submit_approval."
            )
        result = await self._backend.call_tool(
            "browser_snapshot",
            {"target": "html"},
            meta=self._call_meta("browser_snapshot"),
        )
        _raise_for_tool_error("browser_snapshot", result)
        evidence = _text_content(result)
        page_url, page_title = await self._page_identity()
        self._last_snapshot = evidence
        current = self._record_observation(evidence, url=page_url, title=page_title)
        if (
            current.revision != capability.browser_revision
            or current.signature != capability.page_signature
        ):
            self._submission_capability = None
            raise BrowserToolExecutionError(
                "Submission approval was revoked because the reviewed browser state changed. "
                "Inspect, review, and request approval again."
            )

    async def _classify_submit_control(
        self,
        arguments: dict[str, Any],
    ) -> SubmitControlKind:
        """Classify submit behavior from explicit DOM semantics, never button text."""
        target = arguments.get("target")
        if not isinstance(target, str) or not target:
            return SubmitControlKind.NOT_SUBMIT
        try:
            tab = await self._backend._ensure_tab()
            locator = (await tab.resolve_target(target=target)).locator
            classification = await locator.evaluate(
                    """element => {
                        const selector =
                            'button, input[type="submit"], input[type="image"]';
                        let control = element.closest(selector);
                        if (!control) {
                            const clickLayer = element.closest('[role="button"]');
                            if (clickLayer) {
                                const box = clickLayer.getBoundingClientRect();
                                const x = box.left + box.width / 2;
                                const y = box.top + box.height / 2;
                                control = clickLayer.ownerDocument
                                    .elementsFromPoint(x, y)
                                    .find(candidate => candidate !== clickLayer &&
                                        candidate.matches(selector)) || null;
                            }
                        }
                        if (control instanceof HTMLInputElement) {
                            if (control.type !== 'submit' && control.type !== 'image')
                                return 'not_submit';
                        }
                        if (control instanceof HTMLButtonElement) {
                            const type = control.getAttribute('type');
                            if (type !== 'submit' && !(type === null && control.form !== null))
                                return 'not_submit';
                        } else if (!(control instanceof HTMLInputElement)) {
                            return 'not_submit';
                        }

                        const form = control.form || control.closest('form');
                        if (form && (
                            form.getAttribute('role') === 'search' ||
                            form.querySelector('input[type="search"]')
                        )) return 'reversible_search';
                        return 'form_submit';
                    }"""
                )
            if isinstance(classification, bool):
                return (
                    SubmitControlKind.FORM_SUBMIT
                    if classification
                    else SubmitControlKind.NOT_SUBMIT
                )
            try:
                return SubmitControlKind(str(classification))
            except ValueError as exc:
                raise BrowserToolExecutionError(
                    f"Browser returned an unknown submit classification for {target!r}."
                ) from exc
        except BrowserToolExecutionError:
            raise
        except Exception as exc:
            raise BrowserToolExecutionError(
                f"Cannot inspect browser target {target!r}; capture a fresh snapshot and retry."
            ) from exc

    async def _is_form_submit(self, arguments: dict[str, Any]) -> bool:
        """Compatibility predicate for callers that require a final-capable submit."""
        return (
            await self._classify_submit_control(arguments)
            is SubmitControlKind.FORM_SUBMIT
        )

    async def close(self) -> None:
        if getattr(self, "_owns_backend", True):
            await self._backend.close()

    def artifact_path(self, filename: str) -> Path:
        """Return the run-owned path used by browser capture tools."""
        return (self._capture_workspace / filename).resolve()

    @property
    def current_observation(self) -> BrowserObservation | None:
        observation = getattr(self, "_last_observation", None)
        return observation if isinstance(observation, BrowserObservation) else None

    def _current_observation(self) -> BrowserObservation:
        observation = getattr(self, "_last_observation", None)
        if isinstance(observation, BrowserObservation):
            return observation
        snapshot = getattr(self, "_last_snapshot", "")
        return self._record_observation(snapshot if isinstance(snapshot, str) else "")

    def _record_observation(
        self,
        evidence: str,
        *,
        url: str = "",
        title: str = "",
    ) -> BrowserObservation:
        previous = getattr(self, "_last_observation", None)
        if not isinstance(previous, BrowserObservation):
            previous = None
        current_revision = getattr(self, "_browser_revision", 0)
        if not isinstance(current_revision, int):
            current_revision = 0
        candidate = BrowserObservation.create(
            revision=current_revision,
            url=url or (previous.url if previous is not None else ""),
            title=title or (previous.title if previous is not None else ""),
            evidence=evidence,
        )
        if previous is not None and candidate.signature == previous.signature:
            return previous
        revision = current_revision + 1
        observation = BrowserObservation.create(
            revision=revision,
            url=candidate.url,
            title=candidate.title,
            evidence=evidence,
        )
        self._browser_revision = revision
        self._last_observation = observation
        return observation

    def _call_meta(self, name: str) -> dict[str, object]:
        meta: dict[str, object] = {"raw": True}
        if name in INLINE_CAPTURE_TOOLS:
            meta["cwd"] = str(self._capture_workspace)
        return meta

    async def _page_identity(self) -> tuple[str, str]:
        """Read non-critical page identity without making snapshots fail."""
        ensure_tab = getattr(self._backend, "_ensure_tab", None)
        if not callable(ensure_tab):
            return "", ""
        try:
            tab = await ensure_tab()
            page = getattr(tab, "page", None)
            if page is None:
                return "", ""
            title = getattr(page, "title", None)
            return str(getattr(page, "url", "")), str(await title()) if callable(title) else ""
        except Exception:
            return "", ""

    @asynccontextmanager
    async def _operation_scope(self) -> AsyncIterator[None]:
        mutation_gate = getattr(self, "_mutation_gate", None)
        if mutation_gate is not None:
            async with mutation_gate.mutation():
                await self._focus_owned_page()
                yield
            return
        await self._focus_owned_page()
        yield

    async def _focus_owned_page(self) -> None:
        lease = getattr(self, "_lease", None)
        if lease is not None:
            await lease.focus()
        self._assert_owned_page()

    def _assert_owned_page(self) -> None:
        lease = getattr(self, "_lease", None)
        if lease is not None and not lease.owns_current_page():
            raise BrowserToolExecutionError(
                "The assigned run page is unavailable or another run's page became selected."
            )

    async def _discover_owned_popups(self) -> None:
        lease = getattr(self, "_lease", None)
        if lease is not None:
            await lease.discover_owned_popups()


def _text_content(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [getattr(item, "text", None) for item in content]
        return "\n".join(part for part in parts if isinstance(part, str))
    return str(content)


def _raise_for_tool_error(name: str, result: Any) -> None:
    if bool(getattr(result, "is_error", False) or getattr(result, "isError", False)):
        raise BrowserToolExecutionError(f"{name} failed: {_text_content(result)}")


def _content_blocks(result: Any) -> list[dict[str, Any]]:
    content = getattr(result, "content", result)
    if not isinstance(content, list):
        return [{"type": "text", "text": _text_content(result)}]

    blocks: list[dict[str, Any]] = []
    for item in content:
        item_type = getattr(item, "type", None)
        if item_type == "text":
            text = getattr(item, "text", None)
            if isinstance(text, str):
                blocks.append({"type": "text", "text": text})
        elif item_type == "image":
            data = getattr(item, "data", None)
            mime_type = getattr(item, "mimeType", None)
            if isinstance(data, str) and isinstance(mime_type, str):
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{data}",
                        },
                    }
                )
    return blocks or [{"type": "text", "text": _text_content(result)}]
