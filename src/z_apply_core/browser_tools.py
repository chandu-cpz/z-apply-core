from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from inspect import Parameter
from typing import Any, Literal, Protocol, Union, get_args, get_origin

from langchain_core.tools import BaseTool, StructuredTool

ToolCaller = Callable[[str, dict[str, Any]], Awaitable[str]]

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
    "browser_click",
    "browser_type",
    "browser_wait_for",
    "browser_handle_dialog",
    "browser_tabs",
)

VERIFIER_BROWSER_TOOLS = (
    "browser_snapshot",
    "browser_find",
    "browser_tabs",
)


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
    def __init__(self, specs: Sequence[BrowserToolSpec], caller: ToolCaller) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._caller = caller

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if name not in self._specs:
            available = ", ".join(self.names)
            raise ValueError(f"Unknown browser tool {name!r}. Available tools: {available}")
        return await self._caller(name, arguments or {})

    def langchain_tools(self, names: Iterable[str] | None = None) -> list[BaseTool]:
        selected = self.names if names is None else tuple(names)
        return [
            self._to_langchain_tool(self._specs[name])
            for name in selected
            if name in self._specs
        ]

    def _to_langchain_tool(self, spec: BrowserToolSpec) -> BaseTool:
        async def call_tool(**kwargs: Any) -> str:
            return await self.call(spec.name, kwargs)

        return StructuredTool.from_function(
            coroutine=call_tool,
            name=spec.name,
            description=spec.description or spec.title or spec.name,
            args_schema=_tool_schema(spec),
            infer_schema=False,
        )


def _tool_schema(spec: BrowserToolSpec) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in spec.parameters:
        if parameter.hidden:
            continue
        schema = _annotation_schema(parameter.annotation)
        if parameter.description:
            schema["description"] = parameter.description
        properties[parameter.name] = schema
        if parameter.default is Parameter.empty:
            required.append(parameter.name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _annotation_schema(annotation: object) -> dict[str, Any]:
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is None or annotation is type(None):
        return {"type": "null"}

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        return {"enum": list(args)}
    if origin is list:
        item_schema = _annotation_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}
    if origin is dict:
        return {"type": "object"}
    if origin in {Union, type(str | None)}:
        return {"anyOf": [_annotation_schema(option) for option in args]}
    return {"type": "object"}
