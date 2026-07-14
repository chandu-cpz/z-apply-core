from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Protocol, Self
from uuid import uuid4

from langchain_core.tools import ToolException
from playwright_python_mcp.mcp import create_connection

from z_apply_core.browser_config import build_browser_config
from z_apply_core.browser_readiness import FORM_READINESS_SCRIPT, BrowserFormReadiness
from z_apply_core.browser_tools import (
    BROWSER_CHANGING_TOOL_NAMES,
    BrowserToolRegistry,
    normalize_browser_arguments,
)

INLINE_CAPTURE_TOOLS = frozenset({"browser_snapshot", "browser_take_screenshot"})
CORE_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = CORE_ROOT / ".z-apply" / "runs"


class BrowserToolExecutionError(ToolException):
    """A browser backend tool result explicitly marked as an execution error."""


class MutationGate(Protocol):
    def mutation(self) -> AbstractAsyncContextManager[None]: ...


class BrowserLease(Protocol):
    def owns_current_page(self) -> bool: ...

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
        self._approved_submissions = 0
        self._last_snapshot = ""
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
        guarded_submit = False
        if self._submission_guard_active:
            if name == "browser_click":
                guarded_submit = await self._is_form_submit(normalized)
            elif name == "browser_type" and normalized.get("submit") is True:
                guarded_submit = True
            if guarded_submit and self._approved_submissions < 1:
                raise BrowserToolExecutionError(
                    "Final-form submission is locked. Call request_submit_approval and "
                    "wait for an approved result before clicking this submit control."
                )
        if name in BROWSER_CHANGING_TOOL_NAMES:
            async with self._mutation_scope():
                self._assert_owned_page()
                result = await self._backend.call_tool(
                    name,
                    normalized,
                    meta=self._call_meta(name),
                )
                await self._discover_owned_popups()
        else:
            self._assert_owned_page()
            result = await self._backend.call_tool(
                name,
                normalized,
                meta=self._call_meta(name),
            )
        _raise_for_tool_error(name, result)
        if guarded_submit:
            self._approved_submissions -= 1
        text = _text_content(result)
        if name == "browser_snapshot":
            self._last_snapshot = text
        return text

    async def call_tool_content(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        """Return MCP text and image results as LangChain standard content blocks."""
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
        before_snapshot = self._last_snapshot
        mutation = await self.call_tool(name, arguments)
        try:
            evidence = await self.call_tool("browser_snapshot")
        except BrowserToolExecutionError as exc:
            self._last_mutation_signature = signature
            self._last_mutation_made_progress = True
            return f"{mutation}\nPost-action inline snapshot unavailable: {exc}"
        self._last_mutation_signature = signature
        self._last_mutation_made_progress = not before_snapshot or evidence != before_snapshot
        return evidence

    async def upload_files(self, target: str, paths: list[str]) -> str:
        """Resolve an upload trigger to its file input without opening a chooser."""
        async with self._mutation_scope():
            self._assert_owned_page()
            tab = await self._backend._ensure_tab()
            resolved = await tab.resolve_target(target=target)
            locator = resolved.locator
            handle = await locator.evaluate_handle(
                r"""element => {
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

                const controlledIds = [element.getAttribute('for'),
                    element.getAttribute('aria-controls')]
                    .filter(Boolean)
                    .flatMap(value => value.trim().split(/\s+/));
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
            )
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
        return "Files attached directly to the resolved upload control.\n" + evidence

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
        self._assert_owned_page()
        tab = await self._backend._ensure_tab()
        payload = await tab.page.evaluate(FORM_READINESS_SCRIPT)
        return BrowserFormReadiness.from_browser_payload(payload)

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
            tab = await self._backend._ensure_tab()
            locator = (await tab.resolve_target(target=target)).locator
            is_auth_submit = await locator.evaluate(
                """element => {
                const selector = 'button, input[type="submit"], input[type="image"]';
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
                if (!(control instanceof HTMLButtonElement ||
                      control instanceof HTMLInputElement)) return false;
                if (control instanceof HTMLInputElement &&
                    control.type !== 'submit' && control.type !== 'image') return false;
                if (control instanceof HTMLButtonElement) {
                    const type = control.getAttribute('type');
                    if (type !== 'submit' && !(type === null && control.form)) return false;
                }
                const form = control.form || control.closest('form');
                if (!(form instanceof HTMLFormElement)) return false;
                const authInputs = form.querySelectorAll('input');
                return Array.from(authInputs).some(input => {
                    const type = (input.getAttribute('type') || 'text').toLowerCase();
                    const autocomplete = (input.getAttribute('autocomplete') || '')
                        .toLowerCase();
                    return type === 'email' || type === 'password' ||
                        ['username', 'email', 'current-password', 'new-password',
                         'one-time-code'].includes(autocomplete);
                });
            }"""
            )
        except BrowserToolExecutionError:
            raise
        except Exception as exc:
            raise BrowserToolExecutionError(
                "Authentication control is stale or unavailable. Capture a fresh "
                "snapshot and continue from current page evidence."
            ) from exc
        if not is_auth_submit:
            raise BrowserToolExecutionError(
                "Authentication submit rejected: the target is not a submit control in "
                "a structurally identifiable login or verification form."
            )
        async with self._mutation_scope():
            self._assert_owned_page()
            result = await self._backend.call_tool(
                "browser_click",
                {"target": target},
                meta=self._call_meta("browser_click"),
            )
            await self._discover_owned_popups()
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
        async with self._mutation_scope():
            self._assert_owned_page()
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
        self._approved_submissions = 0

    def set_submit_approval(self, approved: bool) -> None:
        """Grant or revoke the one-use form-submit capability."""
        self._approved_submissions = 1 if approved else 0

    async def _is_form_submit(self, arguments: dict[str, Any]) -> bool:
        target = arguments.get("target")
        if not isinstance(target, str) or not target:
            return False
        try:
            tab = await self._backend._ensure_tab()
            locator = (await tab.resolve_target(target=target)).locator
            return bool(
                await locator.evaluate(
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
                            return control.type === 'submit' || control.type === 'image';
                        }
                        if (!(control instanceof HTMLButtonElement)) return false;
                        const type = control.getAttribute('type');
                        return type === 'submit' || (type === null && control.form !== null);
                    }"""
                )
            )
        except BrowserToolExecutionError:
            raise
        except Exception as exc:
            raise BrowserToolExecutionError(
                f"Cannot inspect browser target {target!r}; capture a fresh snapshot and retry."
            ) from exc

    async def close(self) -> None:
        if getattr(self, "_owns_backend", True):
            await self._backend.close()

    def artifact_path(self, filename: str) -> Path:
        """Return the run-owned path used by browser capture tools."""
        return (self._capture_workspace / filename).resolve()

    def _call_meta(self, name: str) -> dict[str, object]:
        meta: dict[str, object] = {"raw": True}
        if name in INLINE_CAPTURE_TOOLS:
            meta["cwd"] = str(self._capture_workspace)
        return meta

    def _mutation_scope(self) -> AbstractAsyncContextManager[None]:
        mutation_gate = getattr(self, "_mutation_gate", None)
        if mutation_gate is not None:
            return mutation_gate.mutation()
        return _unlocked_mutation()

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


@contextlib.asynccontextmanager
async def _unlocked_mutation() -> AsyncIterator[None]:
    yield


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
