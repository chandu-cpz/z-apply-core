"""Gateway table + ModelGateway: resolution, thinking dialects, live switching.

The wire-level opencode Zen tests (tool_choice relaxation, cache breakpoints,
orphan tool-flow pruning) live at the bottom.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from z_apply_core.agents.providers import (
    GATEWAYS,
    REASONING_MODES,
    ModelGateway,
    default_gateway_name,
    get_model_gateway,
    get_provider_catalog,
)
from z_apply_core.agents.providers.gateway import _resolve
from z_apply_core.config import Settings


def patch_settings(**overrides: str):
    values = {
        "MODEL_PROVIDER": "",
        "OGW_API_KEY": "",
        "GROQ_API_KEY": "",
        "ORCA_API_KEY": "",
        "AGNES_API_KEY": "",
        "INFERX_API_KEY": "",
        "OPENCODEGO_API_KEY": "",
    }
    values.update(overrides)
    return patch(
        "z_apply_core.config.load_settings",
        return_value=Settings(**values),
    )


class GatewayResolutionTests(unittest.TestCase):
    def test_registry_lists_gateways_in_detection_order(self) -> None:
        self.assertEqual(
            list(GATEWAYS),
            ["opengateway", "groq", "orca", "agnes", "inferx", "opencodego"],
        )

    def test_explicit_provider_name_wins(self) -> None:
        with patch_settings(INFERX_API_KEY="ix-test"):
            gateway = get_model_gateway(provider_name="inferx")
        self.assertEqual(gateway.name, "inferx")

    def test_model_provider_env_sets_default(self) -> None:
        with patch_settings(MODEL_PROVIDER="inferx", INFERX_API_KEY="ix-test"):
            gateway = get_model_gateway()
        self.assertEqual(gateway.name, "inferx")

    def test_auto_detect_prefers_opengateway_when_key_set(self) -> None:
        with patch_settings(OGW_API_KEY="ogw-test", GROQ_API_KEY="gsk-test"):
            gateway = get_model_gateway()
        self.assertEqual(gateway.name, "opengateway")

    def test_auto_detect_prefers_groq_when_no_opengateway_key(self) -> None:
        with patch_settings(GROQ_API_KEY="gsk-test", AGNES_API_KEY="sk-test"):
            gateway = get_model_gateway()
        self.assertEqual(gateway.name, "groq")

    def test_auto_detect_falls_back_to_orca_when_no_groq_key(self) -> None:
        with patch_settings(ORCA_API_KEY="sk-orca", AGNES_API_KEY="sk-test"):
            gateway = get_model_gateway()
        self.assertEqual(gateway.name, "orca")

    def test_auto_detect_falls_back_to_agnes_when_no_orca_key(self) -> None:
        with patch_settings(AGNES_API_KEY="sk-test", INFERX_API_KEY="ix-test"):
            gateway = get_model_gateway()
        self.assertEqual(gateway.name, "agnes")

    def test_auto_detect_falls_back_to_inferx_when_only_inferx_key(self) -> None:
        with patch_settings(INFERX_API_KEY="ix-test"):
            gateway = get_model_gateway()
        self.assertEqual(gateway.name, "inferx")

    def test_auto_detect_falls_back_to_opencodego_when_only_opencodego_key(self) -> None:
        with patch_settings(OPENCODEGO_API_KEY="sk-test"):
            gateway = get_model_gateway()
        self.assertEqual(gateway.name, "opencodego")

    def test_missing_key_on_explicit_provider_falls_back(self) -> None:
        with patch_settings(MODEL_PROVIDER="inferx", AGNES_API_KEY="sk-test"):
            gateway = get_model_gateway()
        self.assertEqual(gateway.name, "agnes")

    def test_unknown_provider_name_warns_and_falls_back(self) -> None:
        with patch_settings(MODEL_PROVIDER="nope", AGNES_API_KEY="sk-test"):
            gateway = get_model_gateway()
        self.assertEqual(gateway.name, "agnes")

    def test_no_provider_configured_raises(self) -> None:
        with patch_settings(), self.assertRaises(ValueError) as ctx:
            get_model_gateway()
        self.assertIn("MODEL_PROVIDER", str(ctx.exception))

    def test_default_gateway_name_reflects_env(self) -> None:
        with patch_settings(MODEL_PROVIDER="inferx", INFERX_API_KEY="ix-test"):
            self.assertEqual(default_gateway_name(), "inferx")
        # Requested gateway without a key is not "what would be chosen":
        # get_model_gateway would auto-detect a keyed gateway instead.
        with patch_settings(MODEL_PROVIDER="inferx", GROQ_API_KEY="gsk-test"):
            self.assertEqual(default_gateway_name(), "groq")
        with patch_settings(GROQ_API_KEY="gsk-test", AGNES_API_KEY="sk-test"):
            self.assertEqual(default_gateway_name(), "groq")

    def test_default_models_come_from_the_table(self) -> None:
        expected = {
            "opengateway": ("inclusionai/ling-3.0-flash:free", "https://opengateway.gitlawb.com/v1"),
            "groq": ("qwen/qwen3.6-27b", "https://api.groq.com/openai/v1"),
            "orca": ("qwen/qwen3.8-27b-free", "https://api.orcarouter.ai/v1"),
            "agnes": ("agnes-2.0-flash", "https://apihub.agnes-ai.com/v1"),
            "inferx": ("deepseek-v4-flash-0731", "https://model.inferx.net/endpoints/v1"),
            "opencodego": (
                "mimo-v2.5",
                "https://opencode.ai/zen/go/v1",
            ),
        }
        for name, (model, base_url) in expected.items():
            gateway = GATEWAYS[name]
            resolved = _resolve(
                gateway,
                Settings(_env_file=None, **{gateway.env_key: "k"}),
                None,
            )
            self.assertEqual(resolved["model"], model, name)
            self.assertEqual(gateway.base_url, base_url, name)

    def test_explicit_model_override(self) -> None:
        with patch_settings(GROQ_API_KEY="gsk-test"):
            gateway = get_model_gateway(provider_name="groq", model="llama-3.3-70b-versatile")
        self.assertEqual(gateway.model_id, "llama-3.3-70b-versatile")

    def test_get_provider_catalog(self) -> None:
        with patch_settings(GROQ_API_KEY="gsk-test"):
            catalog = get_provider_catalog()
        self.assertEqual(len(catalog), 6)
        groq_entry = next(item for item in catalog if item["name"] == "groq")
        self.assertTrue(groq_entry["configured"])
        self.assertTrue(groq_entry["is_default"])
        self.assertEqual(groq_entry["default_model"], "qwen/qwen3.6-27b")
        ogw_entry = next(item for item in catalog if item["name"] == "opengateway")
        self.assertFalse(ogw_entry["configured"])
        self.assertFalse(ogw_entry["is_default"])


class ClientCachingTests(unittest.TestCase):
    """get_model returns the same object until the config changes."""

    def _gateway(self, name: str = "groq") -> ModelGateway:
        gw = GATEWAYS[name]
        gateway = ModelGateway(
            gateway=gw,
            api_key="k",
            model=gw.default_model,
            default_thinking=True,
            default_effort=None,
        )
        # Per-call effort only steers client choice in auto mode.
        gateway.set_reasoning("auto")
        return gateway

    def test_same_config_returns_same_client(self) -> None:
        gateway = self._gateway()
        self.assertIs(gateway.get_model(), gateway.get_model())

    def test_different_effort_gets_distinct_client(self) -> None:
        gateway = self._gateway("opencodego")
        plain = gateway.get_model()
        low = gateway.get_model(thinking_effort="low")
        self.assertIsNot(plain, low)
        self.assertIs(low, gateway.get_model(thinking_effort="low"))

    def test_switch_invalidates_cache_and_keeps_reasoning_override(self) -> None:
        gateway = self._gateway("opencodego")
        before = gateway.get_model()
        gateway.set_reasoning("on", "high")
        after_switch = gateway.get_model()
        self.assertIsNot(before, after_switch)
        self.assertEqual(gateway.current_reasoning, "on")
        # Override survives a switch to another gateway.
        with patch_settings(AGNES_API_KEY="sk-agnes"):
            gateway.switch("agnes")
        self.assertEqual(gateway.current_reasoning, "on")
        llm = gateway.get_model()
        self.assertEqual(llm.extra_body["chat_template_kwargs"]["enable_thinking"], True)

    def test_switch_rejects_unknown_gateway(self) -> None:
        gateway = self._gateway()
        with self.assertRaises(ValueError):
            gateway.switch("nope")


class ThinkingDialectTests(unittest.TestCase):
    """Each gateway spells thinking its own way; the table encodes that."""

    def _gateway(self, name: str, **overrides: object) -> ModelGateway:
        gw = GATEWAYS[name]
        params: dict[str, object] = {
            "gateway": gw,
            "api_key": "k",
            "model": gw.default_model,
            "default_thinking": True,
            "default_effort": None,
        }
        params.update(overrides)
        gateway = ModelGateway(**params)  # type: ignore[arg-type]
        # Dialect tests exercise the auto path (role policy drives the wire);
        # the product default is now "on"/"high".
        gateway.set_reasoning("auto")
        return gateway

    def test_agnes_chat_template_kwargs(self) -> None:
        llm = self._gateway("agnes").get_model()
        self.assertEqual(llm.extra_body, {"chat_template_kwargs": {"enable_thinking": True}})
        off = self._gateway("agnes", default_thinking=False).get_model()
        self.assertEqual(off.extra_body, {"chat_template_kwargs": {"enable_thinking": False}})

    def test_inferx_enabled_thinking_high_effort_by_default(self) -> None:
        llm = self._gateway("inferx", default_effort="high").get_model()
        self.assertEqual(
            llm.extra_body,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        )

    def test_inferx_off_sends_disabled_thinking(self) -> None:
        llm = self._gateway("inferx", default_thinking=False).get_model()
        self.assertEqual(llm.extra_body, {"thinking": {"type": "disabled"}})

    def test_inferx_requested_effort_clamps_to_supported_range(self) -> None:
        llm = self._gateway("inferx").get_model(thinking_effort="max")
        self.assertEqual(llm.extra_body["reasoning_effort"], "max")
        clamped = self._gateway("inferx").get_model(thinking_effort="bogus")
        self.assertEqual(clamped.extra_body["reasoning_effort"], "high")

    def test_groq_qwen_thinks_with_parsed_format(self) -> None:
        llm = self._gateway("groq").get_model()
        self.assertEqual(llm.reasoning_effort, "default")
        self.assertEqual(llm.reasoning_format, "parsed")
        self.assertEqual(llm.temperature, 0.6)

    def test_groq_off_maps_to_none_effort(self) -> None:
        llm = self._gateway("groq", default_thinking=False).get_model()
        self.assertEqual(llm.reasoning_effort, "none")
        self.assertEqual(llm.temperature, 0.7)

    def test_groq_llama_takes_no_reasoning_args(self) -> None:
        gw = GATEWAYS["groq"]
        gateway = ModelGateway(
            gateway=gw,
            api_key="k",
            model="llama-3.3-70b-versatile",
            default_thinking=True,
            default_effort=None,
        )
        llm = gateway.get_model()
        self.assertIsNone(llm.reasoning_effort)

    def test_orca_has_no_thinking_wire_signal_only_temperature(self) -> None:
        llm = self._gateway("orca").get_model()
        self.assertEqual(llm.temperature, 0.6)
        off = self._gateway("orca", default_thinking=False).get_model()
        self.assertEqual(off.temperature, 0.7)

    def test_opengateway_has_no_thinking_control(self) -> None:
        llm = self._gateway("opengateway").get_model()
        self.assertFalse(hasattr(llm, "reasoning_effort") and llm.reasoning_effort)
        self.assertFalse(getattr(llm, "extra_body", None))

    def test_opencodego_thinking_disabled_by_default(self) -> None:
        gateway = self._gateway("opencodego", default_thinking=False)
        extra = dict(gateway.get_model().extra_body or {})
        self.assertEqual(extra.get("thinking"), {"type": "disabled"})
        self.assertNotIn("reasoning_effort", extra)
        self.assertEqual(extra.get("prompt_cache_key"), "z-apply")
        self.assertEqual(extra.get("prompt_cache_retention"), "24h")

    def test_opencodego_env_flag_enables_thinking(self) -> None:
        gateway = self._gateway("opencodego", default_thinking=False)
        with patch.dict("os.environ", {"OPENCODEGO_THINKING": "1"}, clear=False):
            extra = dict(gateway.get_model().extra_body or {})
        self.assertEqual(extra.get("thinking"), {"type": "enabled"})
        self.assertEqual(extra.get("reasoning_effort"), "high")

    def test_new_gateway_defaults_to_thinking_on_high(self) -> None:
        gw = GATEWAYS["opencodego"]
        gateway = ModelGateway(
            gateway=gw,
            api_key="k",
            model=gw.default_model,
            default_thinking=False,
            default_effort=None,
        )
        extra = dict(gateway.get_model().extra_body or {})
        self.assertEqual(extra.get("thinking"), {"type": "enabled"})
        self.assertEqual(extra.get("reasoning_effort"), "high")

    def test_opencodego_runtime_reasoning_override(self) -> None:
        gateway = self._gateway("opencodego", default_thinking=False)
        gateway.set_reasoning("off")
        self.assertEqual(gateway.get_model().extra_body["thinking"], {"type": "disabled"})
        gateway.set_reasoning("on", "medium")
        extra = dict(gateway.get_model().extra_body or {})
        self.assertEqual(extra.get("thinking"), {"type": "enabled"})
        self.assertEqual(extra.get("reasoning_effort"), "medium")
        gateway.set_reasoning("on")
        self.assertEqual(gateway.get_model().extra_body["reasoning_effort"], "high")
        gateway.set_reasoning("auto")
        with patch.dict("os.environ", {"OPENCODEGO_THINKING": "1"}, clear=False):
            self.assertEqual(gateway.get_model().extra_body["thinking"], {"type": "enabled"})

    def test_per_call_effort_turns_thinking_on_for_that_role_only(self) -> None:
        gateway = self._gateway("opencodego", default_thinking=False)
        role_llm = gateway.get_model(thinking_effort="low")
        self.assertEqual(role_llm.extra_body["reasoning_effort"], "low")
        self.assertEqual(gateway.get_model().extra_body["thinking"], {"type": "disabled"})

    def test_set_reasoning_rejects_unknown_values(self) -> None:
        gateway = self._gateway("groq")
        with self.assertRaises(ValueError):
            gateway.set_reasoning("sometimes")
        with self.assertRaises(ValueError):
            gateway.set_reasoning("on", "extreme")
        self.assertEqual(REASONING_MODES, ("auto", "off", "on"))


class OpenCodeGoToolChoiceTests(unittest.TestCase):
    """DeepSeek V4 thinking mode rejects forced tool_choice (HTTP 400).

    The gateway only accepts "auto"/"none"; langchain's ToolStrategy forces
    "any" for structured output. The client must relax forced selection to
    "auto" so AnswerWriter structured output can run on this gateway.
    """

    def _llm(self):
        gw = GATEWAYS["opencodego"]
        gateway = ModelGateway(
            gateway=gw,
            api_key="k",
            model=gw.default_model,
            default_thinking=False,
            default_effort=None,
        )
        return gateway.get_model()

    def test_bind_tools_relaxes_forced_tool_choice_to_auto(self) -> None:
        from langchain_core.tools import tool

        @tool
        def lookup(query: str) -> dict[str, object]:
            """Search candidate facts."""
            return {"sources": []}

        llm = self._llm()
        for forced in ("any", "required", {"type": "function", "function": {"name": "x"}}):
            bound = llm.bind_tools([lookup], tool_choice=forced)
            self.assertEqual(bound.kwargs.get("tool_choice"), "auto")

    def test_bind_tools_preserves_allowed_tool_choice(self) -> None:
        from langchain_core.tools import tool

        @tool
        def lookup(query: str) -> dict[str, object]:
            """Search candidate facts."""
            return {"sources": []}

        llm = self._llm()
        bound = llm.bind_tools([lookup], tool_choice="auto")
        self.assertEqual(bound.kwargs.get("tool_choice"), "auto")
        unbound = llm.bind_tools([lookup])
        self.assertIsNone(unbound.kwargs.get("tool_choice"))


class OpenCodeGoPayloadTests(unittest.TestCase):
    """Wire-level prefix-cache breakpoints and orphan tool-result filtering.

    Breakpoints are stamped on the outbound payload (where assistant and tool
    blocks survive serialization); orphan tool results are skipped on the wire
    only, so framework history stays byte-stable and the gateway prefix cache
    keeps hitting across turns.
    """

    def _payload(self, messages: list[object]) -> dict[str, object]:
        gw = GATEWAYS["opencodego"]
        gateway = ModelGateway(
            gateway=gw,
            api_key="k",
            model=gw.default_model,
            default_thinking=False,
            default_effort=None,
        )
        return gateway.get_model()._get_request_payload(messages)

    def test_stamps_breakpoints_on_system_tail_and_last_tool(self) -> None:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        payload = self._payload(
            [
                SystemMessage(content="system prompt"),
                HumanMessage(content="first question"),
                AIMessage(
                    content="I will look that up.",
                    tool_calls=[{"name": "lookup", "args": {}, "id": "call-1"}],
                ),
                ToolMessage(content="fact", tool_call_id="call-1"),
                HumanMessage(content="latest question"),
            ]
        )
        messages = payload["messages"]
        marker = {"type": "ephemeral", "ttl": "1h"}
        self.assertEqual(messages[0]["content"][0]["cache_control"], marker)
        self.assertEqual(messages[2]["content"][0]["cache_control"], marker)
        self.assertEqual(messages[4]["content"][0]["cache_control"], marker)
        self.assertEqual(messages[3]["content"][0]["cache_control"], marker)
        self.assertNotIn("cache_control", messages[1]["content"][0])

    def test_unsatisfied_tool_calls_pruned_from_assistant_message(self) -> None:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        payload = self._payload(
            [
                SystemMessage(content="system prompt"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "lookup", "args": {}, "id": "call-kept"},
                        {"name": "lookup", "args": {}, "id": "call-dropped"},
                    ],
                ),
                ToolMessage(content="fact", tool_call_id="call-kept"),
                HumanMessage(content="continue"),
            ]
        )
        messages = payload["messages"]
        assistant = next(m for m in messages if m["role"] == "assistant")
        call_ids = [call.get("id") for call in assistant["tool_calls"]]
        self.assertEqual(call_ids, ["call-kept"])
        tool_ids = [m.get("tool_call_id") for m in messages if m["role"] == "tool"]
        self.assertEqual(tool_ids, ["call-kept"])

    def test_empty_assistant_message_dropped_when_no_tool_result_survives(self) -> None:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        payload = self._payload(
            [
                SystemMessage(content="system prompt"),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "lookup", "args": {}, "id": "call-gone"}],
                ),
                HumanMessage(content="continue"),
            ]
        )
        messages = payload["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])

    def test_orphan_tool_result_dropped_on_the_wire_only(self) -> None:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        payload = self._payload(
            [
                SystemMessage(content="system prompt"),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "lookup", "args": {}, "id": "call-valid"}],
                ),
                ToolMessage(content="orphan result", tool_call_id="call-orphan"),
                ToolMessage(content="fact", tool_call_id="call-valid"),
                HumanMessage(content="continue"),
            ]
        )
        roles = [message["role"] for message in payload["messages"]]
        self.assertEqual(roles, ["system", "assistant", "tool", "user"])
        tool_messages = [m for m in payload["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["content"][0]["text"], "fact")

    def test_stale_cache_control_stripped_before_restamping(self) -> None:
        from langchain_core.messages import HumanMessage, SystemMessage

        payload = self._payload(
            [
                SystemMessage(
                    content=[
                        {
                            "type": "text",
                            "text": "system prompt",
                            "cache_control": {"type": "ephemeral", "ttl": "5m"},
                        }
                    ]
                ),
                HumanMessage(content="hi"),
            ]
        )
        messages = payload["messages"]
        self.assertEqual(
            messages[0]["content"][0]["cache_control"],
            {"type": "ephemeral", "ttl": "1h"},
        )
        self.assertEqual(
            messages[1]["content"][0]["cache_control"],
            {"type": "ephemeral", "ttl": "1h"},
        )


class AgnesMessageCompatTests(unittest.TestCase):
    """Regression guards for the Agnes 400 loop (message-level, no gateway).

    Agnes rejects assistant messages whose content carries reasoning parts.
    langchain-deepseek keeps reasoning in ``additional_kwargs`` and outbound
    serialization drops it from content, so replayed history stays within the
    OpenAI content-part enum even when the turn made tool calls.
    """

    def test_reasoning_surfaces_out_of_content_for_stream_projection(self) -> None:
        from langchain_core.messages import AIMessageChunk

        chunk = AIMessageChunk(
            content="answer",
            additional_kwargs={"reasoning_content": "think step"},
        )
        assert chunk.content == "answer"
        assert chunk.content_blocks == [
            {"type": "reasoning", "reasoning": "think step"},
            {"type": "text", "text": "answer"},
        ]

    def test_reasoned_assistant_replays_without_reasoning_in_content(self) -> None:
        from langchain_core.messages import AIMessage
        from langchain_openai.chat_models.base import _convert_message_to_dict

        message = AIMessage(
            content="Proceeding.",
            additional_kwargs={"reasoning_content": "think step"},
            tool_calls=[
                {
                    "name": "browser_click",
                    "args": {"target": "e140"},
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        )
        body = _convert_message_to_dict(message)
        assert body["role"] == "assistant"
        assert body["content"] == "Proceeding."
        assert body["tool_calls"][0]["function"]["name"] == "browser_click"


if __name__ == "__main__":
    unittest.main()

