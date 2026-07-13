from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from inspect import Parameter
from typing import Annotated, Any, Protocol, cast, get_origin

from langchain_core.tools import BaseTool, StructuredTool, ToolException, tool
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, create_model

TextToolCaller = Callable[[str, dict[str, Any]], Awaitable[str]]
LangChainToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
FileUploader = Callable[[str, list[str]], Awaitable[str]]
AuthSubmitter = Callable[[str], Awaitable[str]]
VerificationLinkOpener = Callable[[str], Awaitable[str]]
_AGENT_TOOL_DESCRIPTIONS: dict[str, str] = {}

INITIAL_AGENT_BROWSER_TOOLS = (
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
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


def make_click_upload_tool(uploader: FileUploader) -> BaseTool:
    """Build a core-only direct file-input upload operation."""

    @tool
    async def browser_click_upload(
        target: str,
        paths: Annotated[list[str], BeforeValidator(_decode_json_container)],
        element: str = "file upload control",
    ) -> str:
        """Attach paths directly to a file control without a native chooser round trip."""
        if not paths or any(not isinstance(path, str) or not path for path in paths):
            raise ValueError("browser_click_upload requires at least one non-empty path.")
        normalized_target = normalize_browser_arguments({"target": target, "element": element}).get(
            "target"
        )
        if not isinstance(normalized_target, str) or not normalized_target:
            raise ValueError("browser_click_upload requires a resolvable target.")
        return await uploader(normalized_target, paths)

    browser_click_upload.handle_tool_error = True
    return browser_click_upload


def make_auth_submit_tool(submitter: AuthSubmitter) -> BaseTool:
    """Build the only submit operation available to AuthenticationSpecialist."""

    @tool
    async def browser_auth_submit(
        target: str,
        element: str = "authentication form submit control",
    ) -> str:
        """Submit a structurally verified login or verification form.

        The executor rejects controls outside a form containing an email,
        username, password, or one-time-code input. This tool never authorizes
        final job-application submission.
        """
        try:
            normalized_target = normalize_browser_arguments(
                {"target": target, "element": element}
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
            _provider_compatible_annotation(parameter.annotation),
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
