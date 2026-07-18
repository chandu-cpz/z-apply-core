from __future__ import annotations

import asyncio
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
from z_apply_core.browser_form_inspection import (
    FormControlBlocker,
    inspect_control,
    inspect_page_blockers,
    inspect_page_capabilities,
    required_file_upload_pending,
)
from z_apply_core.browser_observation import (
    ActionReceipt,
    BrowserCapabilities,
    BrowserControlState,
    BrowserObservation,
)
from z_apply_core.browser_submission import SubmissionGuard
from z_apply_core.browser_targeting import (
    classify_submit_control,
    is_direct_file_upload_trigger,
    resolve_auth_submit_control,
    resolve_file_input,
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
        self._submission = SubmissionGuard()
        self._last_snapshot = ""
        self._last_observation: BrowserObservation | None = None
        self._last_action_receipt: ActionReceipt | None = None
        self._browser_revision = 0
        self._last_mutation_signature = ""
        self._last_mutation_made_progress = True
        self._last_auth_submit_target = ""
        self._last_auth_submit_snapshot = ""
        self._pending_atomic_upload_target = ""
        self._pending_file_chooser: Any | None = None
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
                self._pending_atomic_upload_target = str(normalized.get("target", ""))
                raise BrowserToolExecutionError(
                    "Native file chooser click rejected. Attach the configured file "
                    "atomically with browser_click_upload(target, paths); never click "
                    "a file input or its upload trigger."
                )
            if self._submission.active:
                if name == "browser_click":
                    guarded_submit = (
                        await self._classify_submit_control(normalized)
                        is SubmitControlKind.FORM_SUBMIT
                    )
                elif name == "browser_type" and normalized.get("submit") is True:
                    guarded_submit = True
                if guarded_submit:
                    await self._require_submission_capability_locked(normalized)
            result = await self._call_backend_tool(name, normalized)
            if name in BROWSER_CHANGING_TOOL_NAMES:
                await self._discover_owned_popups()
            if name == "browser_snapshot":
                page_url, page_title = await self._page_identity()
        _raise_for_tool_error(name, result)
        if guarded_submit:
            self._submission.consume()
        text = _text_content(result)
        if name == "browser_snapshot":
            self._last_snapshot = text
            self._record_observation(text, url=page_url, title=page_title)
        return text

    async def _call_backend_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Execute one backend tool while suppressing native file chooser UI."""
        if name != "browser_click":
            return await self._backend.call_tool(
                name,
                arguments,
                meta=self._call_meta(name),
            )

        tab = await self._backend._ensure_tab()
        page = tab.page
        pending_chooser: Any | None = None

        def record_file_chooser(chooser: Any) -> None:
            nonlocal pending_chooser
            pending_chooser = chooser

        page.on("filechooser", record_file_chooser)
        try:
            result = await self._backend.call_tool(
                name,
                arguments,
                meta=self._call_meta(name),
            )
            await asyncio.sleep(0)
        finally:
            page.remove_listener("filechooser", record_file_chooser)

        if pending_chooser is not None:
            self._pending_atomic_upload_target = str(arguments.get("target", ""))
            self._pending_file_chooser = pending_chooser
            raise BrowserToolExecutionError(
                "Native file chooser activation intercepted. Attach the configured "
                "file atomically with browser_click_upload(target, paths); never use "
                "browser_click for an upload trigger."
            )
        return result

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

    async def capture_control_return_evidence(self) -> str:
        """Capture fresh evidence while the workspace gate still blocks agent operations."""
        result = await self._call_backend_tool("browser_snapshot", {"target": "html"})
        _raise_for_tool_error("browser_snapshot", result)
        page_url, page_title = await self._page_identity()
        text = _text_content(result)
        self._last_snapshot = text
        self._record_observation(text, url=page_url, title=page_title)
        return text

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
        receipt = ActionReceipt(
            tool=name,
            arguments=normalized,
            before_revision=before_observation.revision,
            after=after,
            changed=changed,
            result=mutation,
        )
        self._last_action_receipt = receipt
        return receipt.render()

    async def upload_files(self, target: str, paths: list[str]) -> str:
        """Resolve an upload trigger to its file input without opening a chooser."""
        before = self._current_observation()
        pending_chooser = getattr(self, "_pending_file_chooser", None)
        async with self._operation_scope():
            if pending_chooser is not None and target == self._pending_atomic_upload_target:
                await pending_chooser.set_files(paths)
            else:
                tab = await self._backend._ensure_tab()
                resolved = await tab.resolve_target(target=target)
                file_input = await resolve_file_input(tab.page, resolved.locator)
                if file_input is None:
                    raise BrowserToolExecutionError(
                        f"Upload target {target!r} could not be associated with exactly "
                        "one file input. Capture fresh evidence and call "
                        "browser_click_upload on the upload control; never click it to "
                        "open a native chooser."
                    )
                await file_input.set_input_files(paths)
        evidence = await self.call_tool("browser_snapshot")
        after = self._last_observation or self._record_observation(evidence)
        changed = before.signature != after.signature
        self._pending_atomic_upload_target = ""
        self._pending_file_chooser = None
        receipt = ActionReceipt(
            tool="browser_click_upload",
            arguments={"target": target, "paths": paths},
            before_revision=before.revision,
            after=after,
            changed=changed,
            result="Files attached directly to the resolved upload control.",
        )
        self._last_action_receipt = receipt
        return receipt.render()

    @property
    def pending_atomic_upload_target(self) -> str:
        """Target whose activation proved that an atomic upload is required."""
        return self._pending_atomic_upload_target

    @property
    def last_action_receipt(self) -> ActionReceipt | None:
        """Latest typed successful browser mutation evidence for this run."""
        return self._last_action_receipt

    async def _is_file_upload_trigger(self, arguments: dict[str, Any]) -> bool:
        target = arguments.get("target")
        if not isinstance(target, str) or not target:
            return False
        tab = await self._backend._ensure_tab()
        resolved = await tab.resolve_target(target=target)
        return await is_direct_file_upload_trigger(tab.page, resolved.locator)

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

    async def inspect_form_blockers(self) -> tuple[FormControlBlocker, ...]:
        """Capture browser-owned constraint state without asking an LLM to infer it."""
        async with self._operation_scope():
            tab = await self._backend._ensure_tab()
            return await inspect_page_blockers(tab.page)

    async def required_file_upload_pending(self) -> bool:
        """Report whether the live form owns an empty required file input."""
        async with self._operation_scope():
            tab = await self._backend._ensure_tab()
            return await required_file_upload_pending(tab.page)

    async def inspect_capabilities(self) -> BrowserCapabilities:
        """Return compositional structural facts about the current browser page."""
        async with self._operation_scope():
            tab = await self._backend._ensure_tab()
            return await inspect_page_capabilities(tab.page)

    async def inspect_control_state(self, target: str) -> BrowserControlState:
        """Return typed live state for one exact browser-resolved form target."""
        async with self._operation_scope():
            tab = await self._backend._ensure_tab()
            resolved = await tab.resolve_target(target=target)
            return await inspect_control(tab.page, resolved.locator)

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
                submit_control = await resolve_auth_submit_control(tab.page, locator)
                if submit_control is None:
                    raise BrowserToolExecutionError(
                        "Authentication submit rejected: the target is not a submit "
                        "control in a structurally identifiable login or verification form."
                    )
                await submit_control.click(trial=True, timeout=15_000)
                await submit_control.click(timeout=15_000)
                result = "Authentication submit control clicked."
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
        self._submission.activate()

    def set_submit_approval(self, approved: bool) -> None:
        """Approve the pending reviewed capability or revoke it."""
        try:
            self._submission.approve(approved)
        except ValueError as exc:
            raise BrowserToolExecutionError(str(exc)) from exc

    async def prepare_submission_review(self, target: str) -> None:
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
        self._submission.prepare(
            target=normalized_target,
            observation=observation,
        )

    async def _require_submission_capability_locked(
        self,
        arguments: dict[str, Any],
    ) -> None:
        target = arguments.get("target")
        try:
            self._submission.require_target(target)
        except ValueError as exc:
            raise BrowserToolExecutionError(str(exc)) from exc
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
        try:
            self._submission.require_observation(current)
        except ValueError as exc:
            raise BrowserToolExecutionError(str(exc)) from exc

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
            classification, _control = await classify_submit_control(tab.page, locator)
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
