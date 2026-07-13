from __future__ import annotations

from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from langchain_core.tools import ToolException
from playwright_python_mcp.mcp import create_connection

from z_apply_core.browser_config import build_browser_config
from z_apply_core.browser_tools import (
    BROWSER_CHANGING_TOOL_NAMES,
    BrowserToolRegistry,
    normalize_browser_arguments,
)

INLINE_CAPTURE_TOOLS = frozenset(
    {"browser_snapshot", "browser_take_screenshot", "browser_pdf"}
)


class BrowserToolExecutionError(ToolException):
    """A browser backend tool result explicitly marked as an execution error."""


class BrowserSession:
    def __init__(self, server: Any, *, run_id: str) -> None:
        self._server = server
        self._backend = server.backend
        self.run_id = run_id
        self._submission_guard_active = False
        self._approved_submissions = 0
        self._capture_workspace = Path.cwd() / ".z-apply" / "runs" / run_id / "browser-artifacts"
        self.tools = BrowserToolRegistry(
            tuple(server.backend_pool.tools),
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
        result = await self._backend.call_tool(
            name,
            normalized,
            meta=self._call_meta(name),
        )
        _raise_for_tool_error(name, result)
        if guarded_submit:
            self._approved_submissions -= 1
        return _text_content(result)

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
        mutation = await self.call_tool(name, arguments)
        try:
            return await self.call_tool("browser_snapshot")
        except BrowserToolExecutionError as exc:
            return f"{mutation}\nPost-action inline snapshot unavailable: {exc}"

    async def upload_files(self, target: str, paths: list[str]) -> str:
        """Resolve a current ARIA target and attach files without native chooser state."""
        tab = await self._backend._ensure_tab()
        resolved = await tab.resolve_target(target=target)
        locator = resolved.locator
        is_file_input = await locator.evaluate(
            "element => element instanceof HTMLInputElement && element.type === 'file'"
        )
        if not is_file_input:
            file_inputs = locator.locator("input[type=file]")
            count = await file_inputs.count()
            if count != 1:
                raise BrowserToolExecutionError(
                    f"Upload target {target!r} is not a file input and contains "
                    f"{count} file inputs."
                )
            locator = file_inputs
        await locator.set_input_files(paths)
        evidence = await self.call_tool("browser_snapshot")
        return "Files attached directly to the resolved upload control.\n" + evidence

    async def submit_auth_form(self, target: str) -> str:
        """Submit only a form whose live DOM structure proves an auth purpose."""
        tab = await self._backend._ensure_tab()
        locator = (await tab.resolve_target(target=target)).locator
        is_auth_submit = await locator.evaluate(
            """element => {
                const control = element.closest(
                    'button, input[type="submit"], input[type="image"]'
                );
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
        if not is_auth_submit:
            raise BrowserToolExecutionError(
                "Authentication submit rejected: the target is not a submit control in "
                "a structurally identifiable login or verification form."
            )
        result = await self._backend.call_tool(
            "browser_click",
            {"target": target},
            meta=self._call_meta("browser_click"),
        )
        _raise_for_tool_error("browser_click", result)
        try:
            return await self.call_tool("browser_snapshot")
        except BrowserToolExecutionError as exc:
            return f"Authentication form submitted. Post-action snapshot unavailable: {exc}"

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
        tab = await self._backend._ensure_tab()
        locator = (await tab.resolve_target(target=target)).locator
        return bool(
            await locator.evaluate(
                """element => {
                    const control = element.closest(
                        'button, input[type="submit"], input[type="image"]'
                    );
                    if (control instanceof HTMLInputElement) {
                        return control.type === 'submit' || control.type === 'image';
                    }
                    if (!(control instanceof HTMLButtonElement)) return false;
                    const type = control.getAttribute('type');
                    return type === 'submit' || (type === null && control.form !== null);
                }"""
            )
        )

    async def close(self) -> None:
        await self._backend.close()

    def artifact_path(self, filename: str) -> Path:
        """Return the run-owned path used by browser capture tools."""
        return (self._capture_workspace / filename).resolve()

    def _call_meta(self, name: str) -> dict[str, object]:
        meta: dict[str, object] = {"raw": True}
        if name in INLINE_CAPTURE_TOOLS:
            meta["cwd"] = str(self._capture_workspace)
        return meta


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


def _content_blocks(result: Any) -> list[dict[str, str]]:
    content = getattr(result, "content", result)
    if not isinstance(content, list):
        return [{"type": "text", "text": _text_content(result)}]

    blocks: list[dict[str, str]] = []
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
                        "type": "image",
                        "base64": data,
                        "mime_type": mime_type,
                    }
                )
    return blocks or [{"type": "text", "text": _text_content(result)}]
