"""Make deepagents' silent summarization LLM calls visible in the run event stream.

FAIL-006: create_deep_agent auto-adds SummarizationMiddleware OUTSIDE
z-apply-core's middleware chain. When the conversation history crosses the
token trigger, that middleware makes a full LLM call carrying the whole
history BEFORE any lower middleware (including our event-emitting router)
runs — minutes of completely uninstrumented wall clock.

This module installs an observation seam WITHOUT changing summarization
behavior: every model handed to ``create_summarization_middleware`` is wrapped
in :class:`ObservedSummaryModel`, which delegates 1:1 to the real model and
emits started/completed events (with durations, including the error path)
through a run-local observer installed via contextvar.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from contextvars import ContextVar, Token
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from pydantic import PrivateAttr

from z_apply_core.context.token_metric import estimate_messages_tokens

logger = logging.getLogger(__name__)

__all__ = [
    "ObservedSummaryModel",
    "SummarizationObserver",
    "install_summarization_observability",
    "reset_summarization_observer",
    "set_summarization_observer",
]

SummarizationObserver = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]

_observer: ContextVar[SummarizationObserver | None] = ContextVar(
    "z_apply_summarization_observer", default=None
)


def set_summarization_observer(
    observer: SummarizationObserver,
) -> Token[SummarizationObserver | None]:
    """Install the run-local observer; returns a token for restoration."""
    return _observer.set(observer)


def reset_summarization_observer(token: Token[SummarizationObserver | None]) -> None:
    _observer.reset(token)


async def _emit(phase: str, *, status: str = "ok", **data: Any) -> None:
    observer = _observer.get()
    if observer is None:
        return
    try:
        await observer(f"summarization_{phase}", {"status": status, **data})
    except Exception:  # noqa: BLE001 - observability must never break the run
        logger.warning("summarization observer emission failed", exc_info=True)


def _describe(messages: list[BaseMessage]) -> dict[str, Any]:
    return {
        "n_messages": len(messages),
        "chars": sum(len(str(message.content)) for message in messages),
        "est_tokens": estimate_messages_tokens(messages),
    }


class ObservedSummaryModel(BaseChatModel):
    """Transparent proxy around the summarizer's model; emits timing events.

    Delegation contract: every attribute this class does not define falls
    through to the wrapped model, so profile lookups, provider matching, and
    tokenizer behavior are identical to the unwrapped path.
    """

    _inner: BaseChatModel = PrivateAttr()

    def __init__(self, inner: BaseChatModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._inner = inner
        # `profile` is a real pydantic field on BaseChatModel; copy it so
        # trigger-default computation sees exactly what the raw model shows.
        self.profile = getattr(inner, "profile", None)

    def __getattr__(self, item: str) -> Any:
        # Only called when normal lookup fails, i.e. for anything BaseChatModel
        # does not define itself — forward to the wrapped model.
        if item.startswith("_"):
            raise AttributeError(item)
        inner = self._safe_inner()
        if inner is None:
            raise AttributeError(item)
        return getattr(inner, item)

    @property
    def inner_model(self) -> BaseChatModel:
        """The wrapped model (public accessor; pydantic private-attr safe)."""
        inner = self._safe_inner()
        if inner is None:
            raise RuntimeError("ObservedSummaryModel used before initialization")
        return inner

    def _safe_inner(self) -> BaseChatModel | None:
        # Private attrs live in __pydantic_private__; direct access raises
        # AttributeError before __init__ finishes, which would cascade into
        # __getattr__ during pydantic setup.
        private = getattr(self, "__pydantic_private__", None)
        return private.get("_inner") if private else None

    @property
    def _llm_type(self) -> str:
        inner = self._safe_inner()
        if inner is None:
            return "observed-summary-proxy"
        return getattr(inner, "_llm_type", "observed-summary-proxy")

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        inner = self._safe_inner()
        if inner is None:
            raise AttributeError("bind_tools before initialization")
        return inner.bind_tools(tools, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        described = _describe(messages)
        start = time.monotonic()
        result: ChatResult | None = None
        error: Exception | None = None
        try:
            result = self.inner_model._generate(  # noqa: SLF001 - delegation, not tampering
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            return result
        except Exception as exc:
            error = exc
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            completion = _completed_coroutine(duration_ms, described, error)
            if loop is not None:
                loop.create_task(completion)
            else:
                # No running loop (pure-sync caller): drop telemetry rather
                # than block; async paths are the production path.
                logger.debug("summarization call %dms dropped (no loop)", duration_ms)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        described = _describe(messages)
        await _emit("model_call_started", **described)
        start = time.monotonic()
        try:
            result = await self.inner_model._agenerate(  # noqa: SLF001 - delegation, not tampering
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
        except Exception as exc:
            await _emit(
                "model_call_completed",
                status="error",
                duration_ms=int((time.monotonic() - start) * 1000),
                error_type=type(exc).__name__,
                **described,
            )
            raise
        await _emit(
            "model_call_completed",
            duration_ms=int((time.monotonic() - start) * 1000),
            **described,
        )
        return result


async def _completed_coroutine(
    duration_ms: int,
    described: dict[str, Any],
    error: Exception | None,
) -> None:
    if error is not None:
        await _emit(
            "model_call_completed",
            status="error",
            duration_ms=duration_ms,
            error_type=type(error).__name__,
            **described,
        )
    else:
        await _emit("model_call_completed", duration_ms=duration_ms, **described)


_installed = False


def install_summarization_observability() -> None:
    """Patch deepagents so every summarizer it builds uses the observed model.

    Idempotent. Applies to the main agent AND subagent stacks because both are
    built through ``deepagents.graph.create_summarization_middleware``.
    """
    global _installed
    if _installed:
        return
    import deepagents.graph as deepagents_graph  # noqa: PLC0415

    original = deepagents_graph.create_summarization_middleware  # type: ignore[attr-defined]

    def observed_create(model: BaseChatModel, backend: Any, **kwargs: Any) -> Any:
        return original(ObservedSummaryModel(model), backend, **kwargs)

    deepagents_graph.create_summarization_middleware = observed_create  # type: ignore[attr-defined]
    _installed = True
