"""Gateway table: every model backend this project can talk to, as data.

One ``Gateway`` entry replaces what used to be a hand-written provider class.
A gateway is an OpenAI-compatible endpoint plus the answers to four
questions: which chat class builds it, what temperature to use, how it
spells "thinking", and where its key/model come from. Dict order is the
auto-detection priority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ThinkingDialect:
    """How one gateway expresses thinking on the wire.

    ``efforts`` lists the effort levels the gateway accepts; an empty tuple
    means the gateway has no effort concept (or no thinking control at all).
    ``kwargs`` returns the constructor kwargs for the chat class. The base
    dialect is "no thinking control": return nothing, ignore everything.
    """

    efforts: tuple[str, ...] = ()

    def kwargs(self, *, model: str, enabled: bool, effort: str | None) -> dict[str, Any]:
        return {}


class AgnesThinking(ThinkingDialect):
    """Agnes: on/off only, via chat_template_kwargs."""

    def kwargs(self, *, model: str, enabled: bool, effort: str | None) -> dict[str, Any]:
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": enabled}}}


class DeepSeekThinking(ThinkingDialect):
    """DeepSeek-style body fields: thinking type + reasoning_effort.

    Unsupported effort levels clamp to "high" (InferX only accepts
    high/max; a role asking for "low" gets the gateway's floor).
    """

    efforts: tuple[str, ...] = ("high", "max")

    def kwargs(self, *, model: str, enabled: bool, effort: str | None) -> dict[str, Any]:
        if not enabled:
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        resolved = effort if effort in self.efforts else "high"
        return {
            "extra_body": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": resolved,
            }
        }


class ZenThinking(DeepSeekThinking):
    """opencode Zen gateway: same body shape, full effort range.

    Thinking is OFF by default (auto mode): on orchestrator-sized prompts the
    V4 thinking blocks consume the entire output budget (measured: 21.5s
    stream with zero content tokens at max_tokens=2048, versus 3.3s and a
    full answer with thinking off). OPENCODEGO_THINKING=1 or a per-role
    effort request turns it back on; the gateway entry wires both.
    """

    efforts: tuple[str, ...] = ("low", "medium", "high", "max")


class GroqThinking(ThinkingDialect):
    """Groq: native reasoning_effort/reasoning_format constructor args.

    Model-dependent: llama models take no reasoning args at all, gpt-oss
    takes only reasoning_effort, everything else gets parsed format so
    reasoning arrives in additional_kwargs instead of inline think tags
    (Groq rejects raw format when tool use or JSON mode is enabled).
    """

    efforts: tuple[str, ...] = ("low", "medium", "high")

    def kwargs(self, *, model: str, enabled: bool, effort: str | None) -> dict[str, Any]:
        if model.startswith("llama-"):
            return {}
        if effort in self.efforts:
            resolved: str = effort
        elif enabled:
            resolved = "medium" if "gpt-oss" in model else "default"
        else:
            resolved = "none"
        if "gpt-oss" in model:
            return {"reasoning_effort": resolved}
        return {"reasoning_format": "parsed", "reasoning_effort": resolved}


@dataclass(frozen=True)
class Gateway:
    """One OpenAI-compatible model backend."""

    name: str
    description: str
    base_url: str
    env_key: str
    settings_attr: str  # Settings field holding the API key
    model_attr: str  # Settings field holding the model override
    default_model: str
    suggested_models: tuple[str, ...]
    chat_cls: str  # "deepseek" | "openai" | "groq"
    # (thinking, plain) temperature pair; None leaves the chat class default.
    temperatures: tuple[float, float] | None = None
    thinking: ThinkingDialect = field(default_factory=ThinkingDialect)
    reasoning_attr: str | None = None  # Settings bool: auto-mode thinking default
    effort_attr: str | None = None  # Settings str: auto-mode default effort
    # Auto-mode thinking default when the gateway has no reasoning_attr.
    thinking_default: bool = True
    # Env var that flips auto-mode thinking on (opencode Zen: off by default).
    auto_thinking_env: str | None = None
    instant_retry: bool = False  # queue-based gateway: retry immediately, no backoff
    wire_fixups: bool = False  # needs ZenGatewayChat payload surgery + cache fields


GATEWAYS: dict[str, Gateway] = {
    "opengateway": Gateway(
        name="opengateway",
        description="OpenGateway (opengateway.gitlawb.com); free Ling 3.0 Flash by default",
        base_url="https://opengateway.gitlawb.com/v1",
        env_key="OGW_API_KEY",
        settings_attr="ogw_api_key",
        model_attr="ogw_model",
        default_model="inclusionai/ling-3.0-flash:free",
        suggested_models=(
            "inclusionai/ling-3.0-flash:free",
            "nvidia/nemotron-3-ultra:free",
            "deepseek/deepseek-r1:free",
            "qwen/qwen-2.5-coder-32b-instruct",
        ),
        chat_cls="openai",
    ),
    "groq": Gateway(
        name="groq",
        description="Groq hosted endpoints (api.groq.com); ultra-fast Qwen3.6-27B by default",
        base_url="https://api.groq.com/openai/v1",
        env_key="GROQ_API_KEY",
        settings_attr="groq_api_key",
        model_attr="groq_model",
        default_model="qwen/qwen3.6-27b",
        suggested_models=(
            "qwen/qwen3.6-27b",
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "deepseek-r1-distill-llama-70b",
        ),
        chat_cls="groq",
        temperatures=(0.6, 0.7),
        thinking=GroqThinking(),
        reasoning_attr="groq_reasoning",
    ),
    "orca": Gateway(
        name="orca",
        description="OrcaRouter (api.orcarouter.ai); free Qwen3.8-27B by default",
        base_url="https://api.orcarouter.ai/v1",
        env_key="ORCA_API_KEY",
        settings_attr="orca_api_key",
        model_attr="orca_model",
        default_model="qwen/qwen3.8-27b-free",
        suggested_models=(
            "qwen/qwen3.8-27b-free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-r1:free",
        ),
        chat_cls="openai",
        temperatures=(0.6, 0.7),
        reasoning_attr="orca_reasoning",
    ),
    "agnes": Gateway(
        name="agnes",
        description="Free OpenAI-compatible provider (apihub.agnes-ai.com)",
        base_url="https://apihub.agnes-ai.com/v1",
        env_key="AGNES_API_KEY",
        settings_attr="agnes_api_key",
        model_attr="agnes_model",
        default_model="agnes-2.0-flash",
        suggested_models=("agnes-2.0-flash", "agnes-2.0-pro"),
        chat_cls="deepseek",
        temperatures=(0.3, 0.3),
        thinking=AgnesThinking(),
        reasoning_attr="agnes_reasoning",
    ),
    "inferx": Gateway(
        name="inferx",
        description="OpenAI-compatible InferX endpoints (model.inferx.net)",
        base_url="https://model.inferx.net/endpoints/v1",
        env_key="INFERX_API_KEY",
        settings_attr="inferx_api_key",
        model_attr="inferx_model",
        default_model="deepseek-v4-flash-0731",
        suggested_models=("deepseek-v4-flash-0731", "deepseek-v3"),
        chat_cls="deepseek",
        temperatures=(0.3, 0.3),
        thinking=DeepSeekThinking(),
        reasoning_attr="inferx_reasoning",
        effort_attr="inferx_reasoning_effort",
    ),
    "opencodego": Gateway(
        name="opencodego",
        description=(
            "opencode Zen gateway (opencode.ai/zen/go/v1); Muse Spark 1.2 Contributor by default"
        ),
        base_url="https://opencode.ai/zen/go/v1",
        env_key="OPENCODEGO_API_KEY",
        settings_attr="opencodego_api_key",
        model_attr="opencodego_model",
        default_model="muse-spark-1.2-contributor",
        suggested_models=("muse-spark-1.2-contributor", "mimo-v2.5", "deepseek-v4-flash"),
        chat_cls="deepseek",
        temperatures=(0.3, 0.3),
        thinking=ZenThinking(),
        thinking_default=False,
        auto_thinking_env="OPENCODEGO_THINKING",
        instant_retry=True,
        wire_fixups=True,
    ),
}
