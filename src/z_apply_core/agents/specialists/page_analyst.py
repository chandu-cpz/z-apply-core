from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from deepagents import SubAgent, create_deep_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, ToolException
from langgraph.checkpoint.memory import InMemorySaver

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.retry_policy import model_retry_middleware
from z_apply_core.stream_events import FrameworkEventSink

FormPhase = Literal["initial", "filling", "reviewing", "verifying", "submitted"]


@dataclass(frozen=True, slots=True)
class PageAnalysis:
    """One PageAnalyst verdict: the current form phase plus confidence."""

    phase: FormPhase
    confidence: Literal["high", "medium", "low"]
    justification: str


PageClassifier = Callable[[str], Awaitable[PageAnalysis]]


def build_page_analyst(tools: Sequence[BaseTool] = ()) -> SubAgent:
    return cast(
        SubAgent,
        {
            "name": "PageAnalyst",
            "description": (
                "Classify the current application form phase from one page "
                "observation and return a single typed verdict."
            ),
            "system_prompt": load_prompt("page_analyst.md"),
            "tools": list(tools),
            "response_format": ToolStrategy(schema=PageAnalysis),
        },
    )


async def run_page_analysis(
    browser_evidence: str,
    *,
    screenshot: str | None = None,
    classify: PageClassifier | None = None,
    model: BaseChatModel | None = None,
    config: RunnableConfig | None = None,
    sink: FrameworkEventSink | None = None,
) -> PageAnalysis:
    """Run one PageAnalyst classification and return the typed form-phase intent.

    Thin deterministic wrapper around the DeepAgents specialist. ``classify``
    overrides the specialist invocation so deterministic callers and tests can
    inject a fake result; otherwise a standalone structured-output agent runs
    the specialist against the supplied browser evidence.
    """
    if classify is not None:
        return await classify(browser_evidence)
    if model is None or config is None:
        raise ValueError(
            "run_page_analysis requires an injectable classify callable, or a model and config."
        )

    agent = create_deep_agent(
        model=model,
        tools=[],
        system_prompt=load_prompt("page_analyst.md"),
        middleware=[model_retry_middleware()],
        response_format=ToolStrategy(schema=PageAnalysis),
        checkpointer=InMemorySaver(),
    )
    run_config = config.copy()
    configurable = dict(run_config.get("configurable", {}))
    configurable["thread_id"] = f'{configurable.get("thread_id", "z-apply")}:page_analyst'
    run_config["configurable"] = configurable
    stream = await agent.astream_events(
        cast(
            Any,
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _classification_request(
                            browser_evidence,
                            screenshot=screenshot,
                        ),
                    }
                ]
            },
        ),
        config=run_config,
        version="v3",
    )
    result = await consume_deepagent_stream(stream, sink=sink, root_source="PageAnalyst")
    structured = result.output.get("structured_response")
    if not isinstance(structured, PageAnalysis):
        raise ToolException("PageAnalyst ended without a structured PageAnalysis verdict.")
    return structured


def _classification_request(
    browser_evidence: str,
    *,
    screenshot: str | None = None,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Classify the application form phase from the current page "
                "evidence below. Return exactly one structured PageAnalysis. "
                "Do not perform browser actions.\n\n"
                "BEGIN PAGE SNAPSHOT EVIDENCE\n"
                f"{browser_evidence}\n"
                "END PAGE SNAPSHOT EVIDENCE"
            ),
        }
    ]
    if screenshot:
        content.append({"type": "image_url", "image_url": {"url": screenshot}})
    return content
