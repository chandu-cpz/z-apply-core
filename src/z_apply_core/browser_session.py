from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self
from uuid import uuid4

from langchain_core.tools import ToolException
from playwright_python_mcp.mcp import create_connection

from z_apply_core.browser_config import build_browser_config
from z_apply_core.browser_form_inspection import (
    SUBMIT_SELECTOR,
    FormControlBlocker,
    inspect_control,
    inspect_control_options,
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
    MAX_BATCH_STEPS,
    REF_TAG_RE,
    BrowserToolRegistry,
    normalize_browser_arguments,
    validate_bounded_wait_arguments,
)
from z_apply_core.config import CORE_ROOT
from z_apply_core.context.evidence_store import EvidenceStore, render_bounded
from z_apply_core.context.run_context import RunContext
from z_apply_core.profile_pool import PROFILES_ROOT
from z_apply_core.text_utils import collapsed_label

logger = logging.getLogger(__name__)

INLINE_CAPTURE_TOOLS = frozenset({"browser_snapshot", "browser_take_screenshot"})
VISUAL_EVIDENCE_UNAVAILABLE_NOTE = (
    "visual_evidence_unavailable: the browser did not retain a screenshot image "
    "for this run, so no visual evidence exists. Rely on DOM/ARIA evidence or "
    "report that visual evidence is unavailable."
)
ARTIFACT_ROOT = CORE_ROOT / ".z-apply" / "runs"

_CONTROL_LABEL_LINE_PATTERN = re.compile(
    r'^\s*(?:- )?(?:textbox|combobox|checkbox|radio|listbox) "([^"]+)"'
)

#: Batch step action to the backend tool that executes it. ``snapshot`` steps
#: are allowed mid-script but only the final post-batch snapshot records an
#: observation, so mid-script captures never corrupt the revision sequence.
_BATCH_DISPATCH_TOOL_NAMES = {
    "navigate": "browser_navigate",
    "click": "browser_click",
    "type": "browser_type",
    "fill_form": "browser_fill_form",
    "select_option": "browser_select_option",
    "wait_for": "browser_wait_for",
    "handle_dialog": "browser_handle_dialog",
    "evaluate": "browser_evaluate",
    "snapshot": "browser_snapshot",
}


def _control_label_from_line(line: str) -> str | None:
    match = _CONTROL_LABEL_LINE_PATTERN.match(line)
    if match is None:
        return None
    return match.group(1)


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
        run_context: RunContext | None = None,
        evidence_store: EvidenceStore | None = None,
        scratch_profile: Path | None = None,
    ) -> None:
        self._server = server
        self._backend = backend if backend is not None else server.backend
        self._mutation_gate = mutation_gate
        self._lease: BrowserLease | None = None
        self._owns_backend = owns_backend
        self._scratch_profile = scratch_profile
        self.run_id = run_id
        self.run_context = run_context
        self.evidence_store = evidence_store
        self._submission = SubmissionGuard()
        self._screenshot_seq = 0
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
            revision_provider=lambda: self.last_observation_revision,
            receipt_revision_provider=lambda: (
                self._last_action_receipt.after.revision
                if self._last_action_receipt is not None
                else None
            ),
        )

    @classmethod
    async def start(cls, *, run_id: str | None = None, profile_dir: Path | None = None) -> Self:
        resolved_run_id = run_id or uuid4().hex
        if profile_dir is None:
            # Never launch on the sealed master: standalone sessions get a
            # disposable per-run profile dir instead.
            profile_dir = PROFILES_ROOT / f"scratch-{resolved_run_id[:12]}"
        return cls(
            await create_connection(
                build_browser_config(resolved_run_id, profile_dir=profile_dir)
            ),
            run_id=resolved_run_id,
            scratch_profile=profile_dir,
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

    def bind_run_context(self, run_context: RunContext) -> None:
        self.run_context = run_context

    def bind_evidence_store(self, evidence_store: EvidenceStore) -> None:
        self.evidence_store = evidence_store

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        normalized = normalize_browser_arguments(arguments)
        if name == "browser_snapshot" and "target" not in normalized:
            normalized["target"] = "html"
        if name == "browser_take_screenshot":
            normalized = self._ensure_screenshot_filename(normalized)
        try:
            return await self._call_tool_guarded(name, normalized)
        except ToolException:
            raise
        except Exception as exc:
            # The base tool error handler only converts ToolException. A raw
            # browser-layer exception (for example a label mistarget rejected as
            # invalid CSS) must never abort the whole parallel tool-response
            # batch, so it is contained here as a typed tool error instead.
            raise BrowserToolExecutionError(
                f"{name} failed against the current page: {exc}. "
                "Capture fresh browser evidence and retry on a control the "
                "evidence actually shows."
            ) from exc

    async def _guard_click_or_type(self, name: str, arguments: dict[str, Any]) -> bool:
        """Apply the upload + submission guards shared by standalone calls and
        batch steps; return whether a one-use submit approval must be consumed.

        ``browser_click`` on a file input/upload trigger is rejected outright
        (the file must be attached atomically via ``browser_click_upload``), and
        a form-submit click or ``type submit=true`` requires the one-use human
        approval. The caller consumes the approval only after the backend result
        passes its error check, exactly like the standalone path.
        """
        guarded_submit = False
        if name == "browser_click" and await self._is_file_upload_trigger(arguments):
            self._pending_atomic_upload_target = str(arguments.get("target", ""))
            raise BrowserToolExecutionError(
                "Native file chooser click rejected. Attach the file atomically "
                "with browser_click_upload; never click a file input or its "
                "upload trigger."
            )
        if self._submission.active:
            if name == "browser_click":
                guarded_submit = (
                    await self._classify_submit_control(arguments)
                    is SubmitControlKind.FORM_SUBMIT
                )
            elif name == "browser_type" and arguments.get("submit") is True:
                guarded_submit = True
            if guarded_submit:
                await self._require_submission_capability_locked(arguments)
        return guarded_submit

    async def _call_tool_guarded(self, name: str, normalized: dict[str, Any]) -> str:
        page_url = ""
        page_title = ""
        async with self._operation_scope():
            guarded_submit = await self._guard_click_or_type(name, normalized)
            result = await self._call_backend_tool(name, normalized)
            if name in BROWSER_CHANGING_TOOL_NAMES:
                await self._discover_owned_popups()
            if name == "browser_snapshot":
                page_url, page_title = await self._page_identity()
        _raise_for_tool_error(name, result)
        if guarded_submit:
            self._submission.consume()
        text = _text_content(result)
        if name == "browser_snapshot" and normalized.get("target") in (None, "", "html"):
            # Only full-page snapshots define the page observation. A scoped
            # snapshot (target=<ref>) is a subtree view for the model; recording
            # it would corrupt the revision/signature that the capability
            # context keys evidence injection on.
            self._last_snapshot = text
            self._record_observation(text, url=page_url, title=page_title)
        return text

    async def _call_backend_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Execute one backend tool while suppressing native file chooser UI.

        The chooser listener wraps every backend call, not just clicks: an
        upload dropzone can also fire a native chooser through an Enter key,
        a drag, or a programmatic trigger, and any such opening leaves the
        page blocked. Whichever action opens the chooser is intercepted, the
        trigger is recorded as the pending atomic upload target, and the model
        is told to resolve it with ``browser_click_upload``.
        """
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
            target_hint = (
                f"target={self._pending_atomic_upload_target!r}"
                if self._pending_atomic_upload_target
                else "target=<the upload control from fresh evidence>"
            )
            raise BrowserToolExecutionError(
                "Native file chooser intercepted. The action opened an upload "
                f"trigger ({target_hint}); the chooser is pending and blocks other "
                "form tools. Attach the file immediately with "
                "browser_click_upload(target, paths) on that exact control; never "
                "open an upload dropzone with a plain tool call."
            )
        return result

    def _ensure_screenshot_filename(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not arguments.get("filename"):
            arguments["filename"] = f"screenshot_{self._screenshot_seq:03d}.png"
            self._screenshot_seq += 1
        return arguments

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
        evidence_store = getattr(self, "evidence_store", None)
        if evidence_store is not None:
            return f"{result}\n{render_bounded(observation, evidence_store)}"
        return f"{result}\n{observation.compact_render()}"

    async def observe(self, *, full: bool = False) -> str:
        """Return a compact browser state signal, or the full evidence tree when ``full``.

        The model-facing ``browser_observe`` tool uses the compact probe so casual
        state checks never dump the full ARIA tree into the conversation history.
        The full evidence stays available through the ``browser_snapshot`` tool and
        the bounded post-action view arrives automatically via mutation receipts and
        the injected capability context. Internal callers that need the complete
        tree (e.g. the initial run snapshot embedded in the task prompt) pass
        ``full=True``.
        """
        before = self._last_observation
        await self.call_tool("browser_snapshot")
        observation = self._last_observation
        if observation is None:
            raise BrowserToolExecutionError("The browser did not produce current evidence.")
        if full:
            return observation.render()
        changed = before is None or before.signature != observation.signature
        return (
            "BROWSER STATE PROBE\n"
            f"revision: {observation.revision}\n"
            f"changed_since_last_observation: {'true' if changed else 'false'}\n"
            f"signature: {observation.signature[:16]}\n"
            f"url: {observation.url or '(unknown)'}\n"
            f"title: {observation.title or '(untitled)'}\n"
            "This probe carries no page evidence. The bounded post-action view is "
            "injected into your context automatically; call browser_snapshot only "
            "when you need the full accessibility tree.\n"
        )

    @property
    def last_observation_revision(self) -> int | None:
        """Typed revision of the most recent observation, for tool metadata."""
        if self._last_observation is None:
            return None
        return self._last_observation.revision

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
    ) -> list[dict[str, Any]]:
        """Return MCP text and image results as LangChain standard content blocks."""
        normalized = normalize_browser_arguments(arguments)
        if name == "browser_take_screenshot":
            normalized = self._ensure_screenshot_filename(normalized)
        async with self._operation_scope():
            result = await self._backend.call_tool(
                name,
                normalized,
                meta=self._call_meta(name),
            )
        if _is_tool_error(result):
            return [{"type": "text", "text": VISUAL_EVIDENCE_UNAVAILABLE_NOTE}]
        blocks = _content_blocks(result)
        if name == "browser_take_screenshot" and not _has_image_block(blocks):
            return [{"type": "text", "text": VISUAL_EVIDENCE_UNAVAILABLE_NOTE}]
        return blocks

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
        if name == "browser_type":
            await self._pre_select_type_target(normalized)
        before_observation = self._current_observation()
        mutation = await self.call_tool(name, arguments)
        try:
            evidence = await self.call_tool("browser_snapshot")
        except BrowserToolExecutionError as exc:
            self._last_mutation_signature = signature
            self._last_mutation_made_progress = True
            self._last_action_receipt = None
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
        if self.run_context is not None:
            self.run_context.action_log.record(receipt)
        return receipt.render()

    async def run_action_batch(self, steps: list[dict[str, Any]]) -> str:
        """Replay a validated script of browser actions and return one compact receipt.

        Steps execute in order through the same guarded backend path as standalone
        calls: file-upload clicks are rejected, submit-classified clicks require
        the armed one-use human approval, and popup ownership is rediscovered
        after every mutation. Execution stops at the first failing step. A single
        full snapshot after the batch defines the post-batch observation and a
        real ``ActionReceipt``, so platform playbook memory and the no-progress
        guards keep working unchanged.
        """
        if not steps or len(steps) > MAX_BATCH_STEPS:
            raise BrowserToolExecutionError(
                f"browser_batched requires between 1 and {MAX_BATCH_STEPS} steps."
            )
        before = self._current_observation()
        markers: list[str] = []
        stopped: tuple[int, str] | None = None
        async with self._operation_scope():
            for index, raw_step in enumerate(steps):
                if not isinstance(raw_step, Mapping):
                    raise BrowserToolExecutionError(
                        f"browser_batched step {index + 1} is not a mapping."
                    )
                action = str(raw_step.get("action", ""))
                backend_name = _BATCH_DISPATCH_TOOL_NAMES.get(action)
                if backend_name is None:
                    raise BrowserToolExecutionError(
                        f"Unknown browser_batched action {action!r} at step {index + 1}."
                    )
                arguments = {
                    key: value
                    for key, value in normalize_browser_arguments(raw_step).items()
                    if key != "action"
                }
                label = _truncate(_batch_step_label(action, arguments))
                try:
                    result, guarded_submit = await self._run_batch_step(
                        backend_name, action, arguments
                    )
                    _raise_for_tool_error(action, result)
                    if guarded_submit:
                        self._submission.consume()
                    markers.append(f"- {index + 1} {label} ok")
                except Exception as exc:  # noqa: BLE001 - contained per-step like a standalone call
                    markers.append(
                        f"- {index + 1} {label} failed: " f"{_truncate(str(exc))}"
                    )
                    stopped = (index, action)
                    break
            after, note = await self._batch_evidence(before, stopped)
        changed = before.signature != after.signature
        signature = json.dumps(
            {"name": "browser_batched", "arguments": {"steps": steps}},
            sort_keys=True,
            default=str,
        )
        self._last_mutation_signature = signature
        self._last_mutation_made_progress = changed
        receipt = ActionReceipt(
            tool="browser_batched",
            arguments={"steps": steps},
            before_revision=before.revision,
            after=after,
            changed=changed,
            result="\n".join(markers),
        )
        self._last_action_receipt = receipt
        if self.run_context is not None:
            self.run_context.action_log.record(receipt)
        evidence_store = getattr(self, "evidence_store", None)
        if evidence_store is not None:
            evidence = render_bounded(after, evidence_store)
        else:
            evidence = after.compact_render()
        stopped_note = ""
        if stopped is not None:
            stopped_index, stopped_action = stopped
            stopped_note = f", stopped_at: {stopped_index + 1} ({stopped_action})"
        ok_count = sum(1 for marker in markers if marker.endswith(" ok"))
        header = (
            "BROWSER BATCH RECEIPT\n"
            f"steps: {len(steps)} planned, {ok_count} ok{stopped_note}\n"
            f"changed: {'true' if changed else 'false'}\n"
            f"after_revision: {after.revision}\n"
        )
        rendered = f"{header}{''.join(marker + '\n' for marker in markers)}{evidence}{note}"
        if stopped is not None:
            # A stopped batch is a failed script: surface it as a contained tool
            # error (no browser_revision stamp) so sibling mutations in the same
            # response are skipped and bounded evidence re-injects next turn,
            # matching how a failing standalone call behaves.
            raise BrowserToolExecutionError(rendered)
        return rendered

    async def _run_batch_step(
        self,
        backend_name: str,
        action: str,
        arguments: dict[str, Any],
    ) -> tuple[Any, bool]:
        """Execute one batch step through the same guards as a standalone call.

        Returns the raw backend result plus whether a one-use submit approval
        was armed and must be consumed (only after the caller's error check).
        ``snapshot`` steps never record an observation; only the final post-batch
        snapshot does.
        """
        if action == "click":
            guarded_submit = await self._guard_click_or_type("browser_click", arguments)
            result = await self._call_backend_tool(backend_name, arguments)
            return result, guarded_submit
        if action == "type":
            await self._pre_select_type_target(arguments)
            guarded_submit = await self._guard_click_or_type("browser_type", arguments)
            result = await self._call_backend_tool(backend_name, arguments)
            return result, guarded_submit
        if action == "wait_for":
            arguments = validate_bounded_wait_arguments(arguments)
        result = await self._call_backend_tool(backend_name, arguments)
        if backend_name in BROWSER_CHANGING_TOOL_NAMES:
            await self._discover_owned_popups()
        return result, False

    async def _batch_evidence(
        self,
        before: BrowserObservation,
        stopped: tuple[int, str] | None,
    ) -> tuple[BrowserObservation, str]:
        """Capture one post-batch observation, or fall back to the pre-batch state.

        The final snapshot can legitimately fail after a stopped batch (for
        example when a native file chooser is pending and blocks every other
        tool). In that case the pre-batch observation is the best known state and
        the caller appends an evidence-unavailable note; no revision advances.
        """
        try:
            evidence = await self._call_backend_tool("browser_snapshot", {"target": "html"})
            _raise_for_tool_error("browser_snapshot", evidence)
            page_url, page_title = await self._page_identity()
            text = _text_content(evidence)
            return self._record_observation(text, url=page_url, title=page_title), ""
        except Exception as exc:  # noqa: BLE001 - post-action evidence is best-effort
            return before, f"\nPost-action evidence unavailable: {_truncate(str(exc))}"

    async def upload_files(self, target: str, paths: list[str], name: str = "") -> str:
        """Resolve an upload trigger to its file input without opening a chooser.

        ``name`` optionally disambiguates when the page has several hidden empty
        file inputs (e.g. an easy-resume field plus the form's own resume
        field): pass the target input's ``name`` attribute, discoverable via
        browser_evaluate on ``input[type=file]``.
        """
        before = self._current_observation()
        pending_chooser = getattr(self, "_pending_file_chooser", None)
        async with self._operation_scope():
            if pending_chooser is not None and await self._chooser_accepts_target(
                pending_chooser, target
            ):
                await pending_chooser.set_files(paths)
                # The layer recorded the same chooser and blocks every other
                # tool until its modal state is drained (the state survives
                # core's direct set_files).
                await self._drain_layer_choosers()
            else:
                # Clear any layer-recorded chooser first, then attach directly.
                await self._drain_layer_choosers()
                tab = await self._backend._ensure_tab()
                file_input = await self._resolve_upload_file_input(tab, target, name=name)
                if file_input is None:
                    names = await self._file_input_names(tab.page)
                    hint = (
                        f"Pass name={names[0]!r} to browser_click_upload to target the "
                        "input exactly"
                        if len(names) == 1
                        else (
                            "Pass one of the input name/id attributes to "
                            "browser_click_upload to target it exactly: "
                            f"{names}"
                        )
                    )
                    raise BrowserToolExecutionError(
                        f"Upload target {target!r} could not be associated with "
                        "exactly one file input and the page has no unambiguous "
                        f"empty file control. {hint}. Capture fresh evidence and "
                        "retry; never click the control to open a native chooser."
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
        if self.run_context is not None:
            self.run_context.action_log.record(receipt)
        return receipt.render()

    async def _drain_layer_choosers(self) -> None:
        """Clear every file-chooser modal state recorded by the browser layer.

        The layer tracks each open native chooser independently of core's own
        interception and blocks every other tool (including browser_snapshot)
        until its ``browser_file_upload`` tool consumes one; each successful
        call clears exactly one recorded state. Empty ``paths`` skip the
        layer's client-workspace path confinement, so this drains the states
        without re-attaching (core attaches files directly through Playwright
        in ``upload_files``).
        """
        for _ in range(8):
            result = await self._backend.call_tool(
                "browser_file_upload",
                {"paths": []},
                meta=self._call_meta("browser_file_upload"),
            )
            if _is_tool_error(result):
                return

    async def _chooser_accepts_target(self, pending_chooser: Any, target: str) -> bool:
        """True when a pending chooser belongs to the requested upload target.

        The intercepted action records the exact trigger ref, but the model may
        legitimately pass a fresh ref from newer evidence (or a label that maps
        to the same control). Accept the exact recorded ref or any target that
        deterministically resolves to a file input behind the open chooser.
        """
        if target == self._pending_atomic_upload_target:
            return True
        if not target.strip():
            return False
        tab = await self._backend._ensure_tab()
        try:
            resolved = await tab.resolve_target(target=target)
        except Exception:
            return False
        return await resolve_file_input(tab.page, resolved.locator) is not None

    async def _resolve_upload_file_input(
        self, tab: Any, target: str, *, name: str = ""
    ) -> Any | None:
        """Resolve an upload target to its file input without failing on a label.

        The ARIA snapshot often exposes no ref for a hidden or unstyled file
        control, so a model legitimately passes a label or a nearby section ref.
        First resolve the requested target; if that finds a file control, use it.
        Otherwise fall back to the page's single empty file input (the same
        deterministic DOM fact the capability inspector reports). When the page
        has several hidden empty file inputs, the model must disambiguate by the
        input's ``name`` attribute; ambiguity without a name stays an error.
        """
        resolved = None
        if target.strip():
            try:
                resolved = await tab.resolve_target(target=target)
            except Exception:
                resolved = None
        if resolved is not None:
            file_input = await resolve_file_input(tab.page, resolved.locator)
            if file_input is not None:
                return file_input
        empty_inputs = await self._empty_file_inputs(tab.page)
        if name:
            for control in empty_inputs:
                attr_name = await control.get_attribute("name") or ""
                attr_id = await control.get_attribute("id") or ""
                if attr_name == name or attr_id == name:
                    return control
        if len(empty_inputs) == 1:
            return empty_inputs[0]
        return None

    @staticmethod
    async def _empty_file_inputs(page: Any) -> list[Any]:
        """Return enabled file inputs that currently hold no file.

        Mirrors the empty-file-upload fact the capability inspector reports so
        deterministic resolution and browser evidence never disagree. Visibility
        is deliberately not required: boards hide the native control behind a
        styled label, and set_input_files works on a hidden input.
        """
        controls = page.locator('input[type="file"]')
        empty_inputs: list[Any] = []
        for index in range(await controls.count()):
            control = controls.nth(index)
            if await control.is_enabled() and not await control.input_value():
                empty_inputs.append(control)
        return empty_inputs

    @staticmethod
    async def _file_input_names(page: Any) -> list[str]:
        """``name``/``id`` attributes of every empty file input, for disambiguation hints.

        Boards differ: Greenhouse names its inputs ``resume``/``cover_letter``
        via ``id`` with an empty ``name``, while easy-resume and others set
        ``name``. Either attribute identifies the control to
        ``browser_click_upload``.
        """
        controls = page.locator('input[type="file"]')
        names: list[str] = []
        for index in range(await controls.count()):
            control = controls.nth(index)
            if await control.is_enabled() and not await control.input_value():
                attr = await control.get_attribute("name") or await control.get_attribute("id")
                if attr:
                    names.append(attr)
        return names

    async def _pre_select_type_target(self, arguments: dict[str, Any]) -> None:
        """Select a browser_type target's existing text so typing replaces it.

        ``browser_type`` emits keystrokes; without clearing first, a retyped
        value appends to whatever the control already holds (observed as
        concatenated values on React and formatting inputs). Selecting the
        current text makes the incoming keystrokes replace it. Failures are
        non-fatal: the type call itself reports its own errors.
        """
        target = arguments.get("target")
        if not isinstance(target, str) or not target:
            return
        try:
            async with self._operation_scope():
                tab = await self._backend._ensure_tab()
                resolved = await tab.resolve_target(target=target)
                await resolved.locator.select_text()
        except Exception as exc:  # noqa: BLE001 - best-effort pre-clear
            logger.debug("browser_type pre-clear skipped for %r: %s", target, exc)

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

    async def inspect_control_options(self, target: str) -> tuple[str, ...]:
        """Return exact browser-owned options without model transcription."""
        async with self._operation_scope():
            tab = await self._backend._ensure_tab()
            resolved = await tab.resolve_target(target=target)
            return await inspect_control_options(tab.page, resolved.locator)

    async def resolve_control_ref(self, field_label: str) -> str | None:
        """Return the current live ref for the control whose evidence label matches.

        SPA pages re-render between snapshot capture and mutation, which wipes the
        injected ``aria-ref`` attributes that element refs depend on. The request
        ref then fails to resolve even though the control still exists. This
        re-observes and maps the field label back to its current live ref so
        the executor can act on live identity instead of dead evidence.
        """
        label = collapsed_label(field_label)
        if not label:
            return None
        evidence = str(await self.call_tool("browser_snapshot"))
        for line in evidence.splitlines():
            quoted = _control_label_from_line(line)
            if quoted is None or collapsed_label(quoted) != label:
                continue
            match = REF_TAG_RE.search(line)
            if match is not None:
                return match.group(1)
        return None

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

    def submission_consumed(self) -> bool:
        """True when an approved submit click already fired for this approval.

        Lets the orchestrator detect that the previous submission was attempted
        (and therefore did not verifiably succeed, since approval is being
        requested again) so it can require fresh human consent instead of
        silently re-approving.
        """
        return self._submission.is_consumed()

    async def resolve_submit_control_target(self) -> str:
        """Resolve the current enabled form submit control to a snapshot ref.

        The runtime owns this resolution: the model must never have to guess
        the final-submit ref. Candidates come from the typed DOM submit
        selector, are filtered through submit classification (so a form's
        Cancel/Save-draft buttons never qualify), and the first enabled
        form-submit control is captured to a targeted ARIA snapshot whose
        element ref is returned.
        """
        tab = await self._backend._ensure_tab()
        page = tab.page
        locator = page.locator(SUBMIT_SELECTOR)
        count = await locator.count()
        chosen: int | None = None
        for index in range(count):
            item = locator.nth(index)
            if not await item.is_visible():
                continue
            try:
                kind, _control = await classify_submit_control(page, item)
            except Exception:  # noqa: BLE001 - a misbehaving control is skipped
                continue
            if kind != "form_submit":
                continue
            if await item.is_enabled():
                chosen = index
                break
        if chosen is None:
            raise BrowserToolExecutionError(
                "No enabled form submit control is visible. Complete form "
                "validation first, then request the submission review again."
            )
        selector = f"{SUBMIT_SELECTOR} >> nth={chosen}"
        result = await self.call_tool("browser_snapshot", {"target": selector})
        _raise_for_tool_error("browser_snapshot", result)
        match = REF_TAG_RE.search(_text_content(result))
        if match is None:
            raise BrowserToolExecutionError(
                "Resolved submit control has no snapshot ref; capture a fresh snapshot and retry."
            )
        return match.group(1)

    async def submit_approved_application(self) -> str:
        """Resolve and click the current form-submit control under the armed guard.

        The runtime owns final-submit resolution: the control is re-resolved
        from live DOM at call time (never a stale pre-approval ref), classified
        as a real form submit, and clicked through the guarded executor only
        while the human's approval is armed. The click is one-use; the caller
        (Submission Reviewer) reads the returned fresh evidence to judge the
        outcome.
        """
        target = await self.resolve_submit_control_target()
        result = await self.call_tool("browser_click", {"target": target})
        evidence = await self.observe()
        return f"{result}\n{evidence}"

    async def _require_submission_capability_locked(
        self,
        arguments: dict[str, Any],
    ) -> None:
        # The gate is the safety: the human approved final submission for this
        # application and the click is one-use. The clicked control was already
        # classified as a real form submit by the caller; the click's own
        # actionability wait and the reviewer's post-click observe are the
        # outcome checks.
        try:
            self._submission.require_armed()
        except ValueError as exc:
            raise BrowserToolExecutionError(str(exc)) from exc
        if not await self._page_is_alive():
            raise BrowserToolExecutionError(
                "The browser page is frozen (renderer did not answer the "
                "liveness probe). Navigate to the job URL to reload the page, "
                "re-verify the form, and request submission approval again."
            )

    async def _page_is_alive(self) -> bool:
        """Probe renderer liveness through the browser layer (no core JS)."""
        tab = await self._backend._ensure_tab()
        try:
            return bool(await tab.liveness(timeout_seconds=2.0))
        except Exception:
            return False

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
        if getattr(self, "_scratch_profile", None) is not None:
            scratch = self._scratch_profile
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
                self._scratch_profile = None

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


def _batch_step_label(action: str, arguments: dict[str, Any]) -> str:
    if action == "navigate":
        url = arguments.get("url")
        if isinstance(url, str) and url:
            return f"navigate {url}"
        return "navigate"
    target = arguments.get("target")
    if isinstance(target, str) and target:
        return f"{action} {target}"
    if action == "fill_form":
        field_targets = [
            target
            for field in arguments.get("fields", [])
            if isinstance(field, Mapping)
            for target in [field.get("target")]
            if isinstance(target, str) and target
        ]
        if field_targets:
            return f"fill_form {', '.join(field_targets)}"
    return action


def _truncate(text: str, max_chars: int = 200) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _text_content(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [getattr(item, "text", None) for item in content]
        return "\n".join(part for part in parts if isinstance(part, str))
    return str(content)


def _raise_for_tool_error(name: str, result: Any) -> None:
    if _is_tool_error(result):
        raise BrowserToolExecutionError(f"{name} failed: {_text_content(result)}")


def _is_tool_error(result: Any) -> bool:
    return bool(getattr(result, "is_error", False) or getattr(result, "isError", False))


def _has_image_block(blocks: Sequence[dict[str, Any]]) -> bool:
    return any(block.get("type") == "image_url" for block in blocks)


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
