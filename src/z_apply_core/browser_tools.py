from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from inspect import Parameter
from typing import Any, Protocol, cast

from langchain_core.tools import BaseTool, StructuredTool, ToolException, tool
from pydantic import BaseModel, ConfigDict, Field, create_model

TextToolCaller = Callable[[str, dict[str, Any]], Awaitable[str]]
LangChainToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
_AGENT_TOOL_DESCRIPTIONS = {
    "browser_file_upload": (
        "Upload files into the currently open native file chooser. This tool has no "
        "target or selector argument. First open the chooser with browser_click, then "
        "immediately call browser_file_upload with paths as a JSON array of absolute "
        'paths, for example paths=["/absolute/resume.pdf"].'
    ),
}

INITIAL_AGENT_BROWSER_TOOLS = (
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_file_upload",
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

VERIFIER_BROWSER_TOOLS = (
    "browser_snapshot",
    "browser_find",
)

BROWSER_CHANGING_TOOL_NAMES = frozenset(
    {
        "browser_click",
        "browser_click_upload",
        "browser_type",
        "browser_fill_form",
        "browser_select_option",
        "browser_file_upload",
        "browser_handle_dialog",
    }
)


def normalize_browser_arguments(
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Translate agent-facing ARIA reference notation at the browser boundary."""
    normalized = dict(arguments or {})
    normalized["target"] = _normalize_target(
        normalized.get("target"),
        element=normalized.get("element"),
    )
    if normalized.get("target") is None:
        normalized.pop("target", None)

    fields = normalized.get("fields")
    if isinstance(fields, list):
        normalized["fields"] = [
            {
                **field,
                "target": _normalize_target(
                    field.get("target"),
                    element=field.get("name"),
                ),
            }
            if isinstance(field, Mapping)
            else field
            for field in fields
        ]
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
    if len(candidate) > 1 and candidate[0] == "e" and candidate[1:].isdigit():
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


def make_click_upload_tool(caller: TextToolCaller) -> BaseTool:
    """Build a core-only atomic click-to-file-upload operation."""

    @tool
    async def browser_click_upload(
        target: str,
        paths: list[str],
        element: str = "file upload control",
    ) -> str:
        """Click a file control and upload paths through its native chooser atomically."""
        if not paths or any(not isinstance(path, str) or not path for path in paths):
            raise ValueError("browser_click_upload requires at least one non-empty path.")
        click_arguments = normalize_browser_arguments({"target": target})
        if element:
            click_arguments["element"] = element
        await caller("browser_click", click_arguments)
        uploaded = await caller("browser_file_upload", {"paths": paths})
        try:
            evidence = await caller("browser_snapshot", {})
        except ToolException as exc:
            evidence = f"Post-upload inline snapshot unavailable: {exc}"
        return "Atomic file-control click and upload completed.\n" + uploaded + "\n" + evidence

    browser_click_upload.handle_tool_error = True
    return browser_click_upload


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
    ) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._caller = caller
        self._langchain_callers = dict(langchain_callers or {})

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
        async def call_tool(**kwargs: Any) -> Any:
            caller = self._langchain_callers.get(spec.name, self._caller)
            arguments = {k: v for k, v in kwargs.items() if v is not None and v != ""}
            return await caller(spec.name, normalize_browser_arguments(arguments))

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
            parameter.annotation,
            Field(default=default, description=parameter.description),
        )
    model_name = "".join(part.title() for part in spec.name.split("_")) + "Arguments"
    model_factory = cast(Any, create_model)
    return cast(
        type[BaseModel],
        model_factory(
            model_name,
            __config__=ConfigDict(extra="forbid"),
            **fields,
        ),
    )
