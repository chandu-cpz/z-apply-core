from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from z_apply_core.agents.model_provider import (
    AgnesProvider,
    GroqProvider,
    InferXProvider,
    OpenCodeGoProvider,
    OpenGatewayProvider,
    OrcaProvider,
    SwitchableModelProvider,
    default_provider_name,
    get_provider,
    get_provider_catalog,
    list_providers,
)
from z_apply_core.agents.router_middleware import ModelRouter
from z_apply_core.config import Settings


class ProviderSelectionTests(unittest.TestCase):
    def patch_settings(self, **overrides: str) -> patch:
        values = {
            "MODEL_PROVIDER": "",
            "OGW_API_KEY": "",
            "OGW_MODEL": "inclusionai/ling-3.0-flash:free",
            "GROQ_API_KEY": "",
            "GROQ_MODEL": "qwen/qwen3.6-27b",
            "ORCA_API_KEY": "",
            "ORCA_MODEL": "qwen/qwen3.8-27b-free",
            "AGNES_API_KEY": "",
            "INFERX_API_KEY": "",
            "INFERX_MODEL": "deepseek-v4-flash-0731",
            "OPENCODEGO_API_KEY": "",
            "OPENCODEGO_MODEL": "muse-spark-1.2-contributor",
        }
        values.update(overrides)
        return patch(
            "z_apply_core.config.load_settings",
            return_value=Settings(**values),
        )

    def test_explicit_provider_name_wins(self) -> None:
        with self.patch_settings(INFERX_API_KEY="ix-test"):
            provider = get_provider(provider_name="inferx")

        self.assertIsInstance(provider, InferXProvider)

    def test_model_provider_env_sets_default(self) -> None:
        with self.patch_settings(MODEL_PROVIDER="inferx", INFERX_API_KEY="ix-test"):
            provider = get_provider()

        self.assertIsInstance(provider, InferXProvider)

    def test_auto_detect_prefers_opengateway_when_key_set(self) -> None:
        with self.patch_settings(OGW_API_KEY="ogw-test", GROQ_API_KEY="gsk-test"):
            provider = get_provider()

        self.assertIsInstance(provider, OpenGatewayProvider)

    def test_auto_detect_prefers_groq_when_no_opengateway_key(self) -> None:
        with self.patch_settings(GROQ_API_KEY="gsk-test", AGNES_API_KEY="sk-test"):
            provider = get_provider()

        self.assertIsInstance(provider, GroqProvider)

    def test_auto_detect_falls_back_to_orca_when_no_groq_key(self) -> None:
        with self.patch_settings(ORCA_API_KEY="sk-orca-test", AGNES_API_KEY="sk-test"):
            provider = get_provider()

        self.assertIsInstance(provider, OrcaProvider)

    def test_auto_detect_falls_back_to_agnes_when_no_orca_key(self) -> None:
        with self.patch_settings(AGNES_API_KEY="sk-test", INFERX_API_KEY="ix-test"):
            provider = get_provider()

        self.assertIsInstance(provider, AgnesProvider)

    def test_auto_detect_falls_back_to_inferx_when_only_inferx_key(self) -> None:
        with self.patch_settings(INFERX_API_KEY="ix-test"):
            provider = get_provider()

        self.assertIsInstance(provider, InferXProvider)

    def test_auto_detect_falls_back_to_opencodego_when_only_opencodego_key(
        self,
    ) -> None:
        with self.patch_settings(OPENCODEGO_API_KEY="sk-test"):
            provider = get_provider()

        self.assertIsInstance(provider, OpenCodeGoProvider)

    def test_missing_key_on_explicit_provider_falls_back(self) -> None:
        with self.patch_settings(MODEL_PROVIDER="inferx", AGNES_API_KEY="sk-test"):
            provider = get_provider()

        self.assertIsInstance(provider, AgnesProvider)

    def test_unknown_provider_name_warns_and_falls_back(self) -> None:
        with self.patch_settings(MODEL_PROVIDER="nope", AGNES_API_KEY="sk-test"):
            provider = get_provider()

        self.assertIsInstance(provider, AgnesProvider)

    def test_no_provider_configured_raises(self) -> None:
        with self.patch_settings(), self.assertRaises(ValueError) as ctx:
            get_provider()

        self.assertIn("MODEL_PROVIDER", str(ctx.exception))

    def test_registry_lists_providers_in_detection_order(self) -> None:
        names = [spec.name for spec in list_providers()]

        self.assertEqual(
            names,
            ["opengateway", "groq", "orca", "agnes", "inferx", "opencodego"],
        )

    def test_default_provider_name_reflects_env(self) -> None:
        with self.patch_settings(MODEL_PROVIDER="inferx"):
            self.assertEqual(default_provider_name(), "inferx")
        with self.patch_settings(GROQ_API_KEY="gsk-test", AGNES_API_KEY="sk-test"):
            self.assertEqual(default_provider_name(), "groq")

    def test_orca_provider_default_model(self) -> None:
        provider = OrcaProvider(api_key="sk-orca-test")

        self.assertEqual(provider._model, "qwen/qwen3.8-27b-free")
        self.assertEqual(provider.BASE_URL, "https://api.orcarouter.ai/v1")

    def test_inferx_provider_default_model(self) -> None:
        provider = InferXProvider(api_key="ix-test")

        self.assertEqual(provider._model, "deepseek-v4-flash-0731")
        self.assertEqual(provider.BASE_URL, "https://model.inferx.net/endpoints/v1")

    def test_groq_provider_default_model(self) -> None:
        provider = GroqProvider(api_key="gsk-test")

        self.assertEqual(provider._model, "qwen/qwen3.6-27b")
        self.assertEqual(provider.BASE_URL, "https://api.groq.com/openai/v1")

    def test_opengateway_provider_default_model(self) -> None:
        provider = OpenGatewayProvider(api_key="ogw-test")

        self.assertEqual(provider._model, "inclusionai/ling-3.0-flash:free")
        self.assertEqual(provider.BASE_URL, "https://opengateway.gitlawb.com/v1")

    def test_opencodego_provider_default_model(self) -> None:
        provider = OpenCodeGoProvider(api_key="sk-test")

        self.assertEqual(provider._model, "muse-spark-1.2-contributor")
        self.assertEqual(provider.BASE_URL, "https://opencode.ai/zen/go/v1")

    def test_get_provider_catalog(self) -> None:
        with self.patch_settings(GROQ_API_KEY="gsk-test"):
            catalog = get_provider_catalog()

        self.assertEqual(len(catalog), 6)
        groq_entry = next(item for item in catalog if item["name"] == "groq")
        self.assertTrue(groq_entry["configured"])
        self.assertTrue(groq_entry["is_default"])
        self.assertEqual(groq_entry["default_model"], "qwen/qwen3.6-27b")
        self.assertIn("llama-3.3-70b-versatile", groq_entry["suggested_models"])

        opengateway_entry = next(item for item in catalog if item["name"] == "opengateway")
        self.assertFalse(opengateway_entry["configured"])
        self.assertFalse(opengateway_entry["is_default"])

    def test_explicit_model_override(self) -> None:
        with self.patch_settings(GROQ_API_KEY="gsk-test"):
            provider = get_provider(provider_name="groq", model="llama-3.3-70b-versatile")

        self.assertIsInstance(provider, GroqProvider)
        self.assertEqual(provider._model, "llama-3.3-70b-versatile")

    def test_switchable_model_provider(self) -> None:
        with self.patch_settings(GROQ_API_KEY="gsk-test", AGNES_API_KEY="sk-agnes"):
            groq_p = get_provider(provider_name="groq")
            switchable = SwitchableModelProvider(groq_p, initial_name="groq", initial_model="qwen/qwen3.6-27b")
            self.assertEqual(switchable.current_provider_name, "groq")
            self.assertEqual(switchable.current_model, "qwen/qwen3.6-27b")

            switchable.switch("agnes", model="agnes-2.0-pro")
            self.assertEqual(switchable.current_provider_name, "agnes")
            self.assertEqual(switchable.current_model, "agnes-2.0-pro")
            self.assertIsInstance(switchable._provider, AgnesProvider)
            self.assertEqual(switchable._provider._model, "agnes-2.0-pro")


if __name__ == "__main__":
    unittest.main()


class OpenCodeGoToolChoiceTests(unittest.TestCase):
    """DeepSeek V4 thinking mode rejects forced tool_choice (HTTP 400).

    The opencode Zen gateway only accepts "auto"/"none"; langchain's
    ToolStrategy forces "any" for structured output. The provider must
    relax forced tool selection to "auto" so AnswerWriter structured
    output can run on this gateway.
    """

    def test_bind_tools_relaxes_forced_tool_choice_to_auto(self) -> None:
        from langchain_core.tools import tool

        from z_apply_core.agents.model_provider import OpenCodeGoProvider

        @tool
        def lookup_candidate_memory(query: str) -> dict[str, object]:
            """Search candidate facts."""
            return {"sources": []}

        provider = OpenCodeGoProvider(api_key="sk-test")
        selection = asyncio.run(provider.lease())
        llm = selection.llm
        for forced in ("any", "required", {"type": "function", "function": {"name": "x"}}):
            bound = llm.bind_tools([lookup_candidate_memory], tool_choice=forced)
            self.assertEqual(bound.kwargs.get("tool_choice"), "auto")

    def test_bind_tools_preserves_allowed_tool_choice(self) -> None:
        from langchain_core.tools import tool

        from z_apply_core.agents.model_provider import OpenCodeGoProvider

        @tool
        def lookup_candidate_memory(query: str) -> dict[str, object]:
            """Search candidate facts."""
            return {"sources": []}

        provider = OpenCodeGoProvider(api_key="sk-test")
        selection = asyncio.run(provider.lease())
        llm = selection.llm
        bound = llm.bind_tools([lookup_candidate_memory], tool_choice="auto")
        self.assertEqual(bound.kwargs.get("tool_choice"), "auto")
        bound = llm.bind_tools([lookup_candidate_memory])
        self.assertIsNone(bound.kwargs.get("tool_choice"))


class OpenCodeGoThinkingTests(unittest.TestCase):
    """V4 thinking streams eat the output budget; default must be off."""

    def _extra_body(self, env_patch: dict[str, str]) -> dict[str, object]:
        import asyncio

        from z_apply_core.agents.model_provider import OpenCodeGoProvider

        with patch.dict("os.environ", env_patch, clear=False):
            selection = asyncio.run(OpenCodeGoProvider(api_key="sk-test").lease())
        return dict(selection.llm.extra_body or {})

    def test_thinking_disabled_by_default(self) -> None:
        extra = self._extra_body({})
        self.assertEqual(extra.get("thinking"), {"type": "disabled"})
        self.assertNotIn("reasoning_effort", extra)

    def test_thinking_enabled_via_env(self) -> None:
        extra = self._extra_body({"OPENCODEGO_THINKING": "1"})
        self.assertEqual(extra.get("thinking"), {"type": "enabled"})
        self.assertEqual(extra.get("reasoning_effort"), "high")

    def test_set_reasoning_off_forces_thinking_off(self) -> None:
        provider = OpenCodeGoProvider(api_key="sk-test")
        provider.set_reasoning("off")
        selection = asyncio.run(provider.lease(reasoning_effort="high"))
        extra = dict(selection.llm.extra_body or {})
        self.assertEqual(extra.get("thinking"), {"type": "disabled"})

    def test_set_reasoning_on_enables_thinking_with_effort(self) -> None:
        provider = OpenCodeGoProvider(api_key="sk-test")
        provider.set_reasoning("on", "medium")
        selection = asyncio.run(provider.lease())
        extra = dict(selection.llm.extra_body or {})
        self.assertEqual(extra.get("thinking"), {"type": "enabled"})
        self.assertEqual(extra.get("reasoning_effort"), "medium")

    def test_set_reasoning_on_defaults_to_high_effort(self) -> None:
        provider = OpenCodeGoProvider(api_key="sk-test")
        provider.set_reasoning("on")
        selection = asyncio.run(provider.lease())
        extra = dict(selection.llm.extra_body or {})
        self.assertEqual(extra.get("reasoning_effort"), "high")

    def test_set_reasoning_auto_restores_env_default(self) -> None:
        provider = OpenCodeGoProvider(api_key="sk-test")
        provider.set_reasoning("on", "high")
        provider.set_reasoning("auto")
        with patch.dict("os.environ", {"OPENCODEGO_THINKING": "1"}, clear=False):
            selection = asyncio.run(provider.lease())
        extra = dict(selection.llm.extra_body or {})
        self.assertEqual(extra.get("thinking"), {"type": "enabled"})

    def test_reasoning_effort_param_still_wins_in_auto_mode(self) -> None:
        provider = OpenCodeGoProvider(api_key="sk-test")
        provider.set_reasoning("auto")
        selection = asyncio.run(provider.lease(reasoning_effort="low"))
        extra = dict(selection.llm.extra_body or {})
        self.assertEqual(extra.get("thinking"), {"type": "enabled"})
        self.assertEqual(extra.get("reasoning_effort"), "low")

    def test_cache_fields_survive_thinking_toggle(self) -> None:
        extra = self._extra_body({})
        self.assertEqual(extra.get("prompt_cache_key"), "z-apply")
        self.assertEqual(extra.get("prompt_cache_retention"), "24h")


class SwitchableReasoningTests(unittest.TestCase):
    """Runtime reasoning override on the switchable proxy."""

    def test_set_reasoning_delegates_and_tracks_state(self) -> None:
        switchable = SwitchableModelProvider(
            OpenCodeGoProvider(api_key="sk-test"),
            initial_name="opencodego",
            initial_model="mimo-v2.5",
        )
        switchable.set_reasoning("on", "medium")
        self.assertEqual(switchable.current_reasoning, "on")
        self.assertEqual(switchable.current_reasoning_effort, "medium")
        self.assertEqual(switchable._provider._reasoning_mode, "on")
        self.assertEqual(switchable._provider._reasoning_effort_override, "medium")

    def test_set_reasoning_auto(self) -> None:
        switchable = SwitchableModelProvider(
            OpenCodeGoProvider(api_key="sk-test"),
            initial_name="opencodego",
            initial_model="mimo-v2.5",
        )
        switchable.set_reasoning("auto")
        self.assertEqual(switchable.current_reasoning, "auto")
        self.assertEqual(switchable.current_reasoning_effort, None)

    def test_set_reasoning_rejects_unknown_mode(self) -> None:
        switchable = SwitchableModelProvider(
            OpenCodeGoProvider(api_key="sk-test"),
            initial_name="opencodego",
            initial_model="mimo-v2.5",
        )
        with self.assertRaises(ValueError):
            switchable.set_reasoning("sometimes")
        with self.assertRaises(ValueError):
            switchable.set_reasoning("on", "extreme")

    def test_reasoning_override_survives_switch(self) -> None:
        switchable = SwitchableModelProvider(
            OpenCodeGoProvider(api_key="sk-test"),
            initial_name="opencodego",
            initial_model="mimo-v2.5",
        )
        switchable.set_reasoning("on", "high")
        with patch.dict("os.environ", {"AGNES_API_KEY": "sk-agnes"}, clear=False):
            switchable.switch("agnes")
        self.assertEqual(switchable.current_reasoning, "on")
        self.assertIsInstance(switchable._provider, AgnesProvider)
        self.assertTrue(switchable._provider._reasoning)


class SwitchableEpochTests(unittest.TestCase):
    """Provider epoch bumps so routers can detect a live switch."""

    def _switchable(self) -> SwitchableModelProvider:
        return SwitchableModelProvider(
            OpenCodeGoProvider(api_key="sk-test"),
            initial_name="opencodego",
            initial_model="mimo-v2.5",
        )

    def test_epoch_starts_zero(self) -> None:
        self.assertEqual(self._switchable().epoch, 0)

    def test_epoch_bumps_on_switch_and_set_reasoning(self) -> None:
        switchable = self._switchable()
        switchable.set_reasoning("on", "high")
        self.assertEqual(switchable.epoch, 1)
        with patch.dict("os.environ", {"AGNES_API_KEY": "sk-agnes"}, clear=False):
            switchable.switch("agnes")
        self.assertEqual(switchable.epoch, 2)


class ModelRouterReleasesOnSwitchTests(unittest.TestCase):
    """The router must re-lease and drive the request with the new model when
    the switchable provider's epoch changes mid-run, and must NOT re-lease
    otherwise (free-tier caching preserved)."""

    class FakeProvider:
        epoch = 0

        def __init__(self) -> None:
            self.leases = 0
            self.models: list[str] = []

        async def lease(self, **kwargs: object) -> object:
            self.leases += 1
            model_id = f"model-{self.leases}"
            self.models.append(model_id)
            return SimpleNamespace(
                info=SimpleNamespace(id=model_id),
                llm=SimpleNamespace(model=model_id),
            )

        def record_failure(self, model_id: str, **kwargs: object) -> None:
            del model_id, kwargs

        def cooldown_model(self, model_id: str, seconds: float) -> None:
            del model_id, seconds

    def _request(self) -> object:
        from langchain.agents.middleware.types import ModelRequest

        return ModelRequest(
            model=SimpleNamespace(model="pinned"),
            messages=[HumanMessage(content="hi")],
            tools=[],
            response_format=None,
            state={},
            runtime=None,
            tool_choice=None,
        )

    def test_leases_once_without_switch(self) -> None:
        from langchain.agents.middleware.types import ModelRequest

        provider = self.FakeProvider()
        router = ModelRouter(provider, role="test", selection=None)
        seen: list[str] = []

        async def handler(request: ModelRequest[object]) -> object:
            seen.append(request.model.model)
            return SimpleNamespace(result=["ok"])

        asyncio.run(router.awrap_model_call(self._request(), handler))
        asyncio.run(router.awrap_model_call(self._request(), handler))

        self.assertEqual(provider.leases, 1)
        self.assertEqual(seen, ["pinned", "pinned"])

    def test_releases_and_swaps_model_after_epoch_change(self) -> None:
        from langchain.agents.middleware.types import ModelRequest

        provider = self.FakeProvider()
        router = ModelRouter(provider, role="test", selection=None)
        seen: list[str] = []

        async def handler(request: ModelRequest[object]) -> object:
            seen.append(request.model.model)
            return SimpleNamespace(result=["ok"])

        # First call leases model-1 but leaves the pinned graph model untouched.
        asyncio.run(router.awrap_model_call(self._request(), handler))
        self.assertEqual(provider.leases, 1)
        self.assertEqual(seen, ["pinned"])

        # A mid-run switch bumps the epoch: next call re-leases and the freshly
        # leased model drives the request from then on.
        provider.epoch = 1
        asyncio.run(router.awrap_model_call(self._request(), handler))
        self.assertEqual(provider.leases, 2)
        self.assertEqual(seen, ["pinned", "model-2"])

        # No further switch: no re-lease, but the new model keeps driving calls.
        asyncio.run(router.awrap_model_call(self._request(), handler))
        self.assertEqual(provider.leases, 2)
        self.assertEqual(seen, ["pinned", "model-2", "model-2"])

    def test_switch_with_preselected_router_releases_on_epoch_change(self) -> None:
        from langchain.agents.middleware.types import ModelRequest

        provider = self.FakeProvider()
        initial = SimpleNamespace(
            info=SimpleNamespace(id="preselected"),
            llm=SimpleNamespace(model="preselected"),
        )
        router = ModelRouter(provider, role="test", selection=initial)
        seen: list[str] = []

        async def handler(request: ModelRequest[object]) -> object:
            seen.append(request.model.model)
            return SimpleNamespace(result=["ok"])

        # Preselected router: the passed selection is reused for telemetry, no
        # lease happens, and the request still uses the graph's pinned model.
        asyncio.run(router.awrap_model_call(self._request(), handler))
        self.assertEqual(provider.leases, 0)
        self.assertEqual(seen, ["pinned"])

        # After a switch the router re-leases and overrides the request model.
        provider.epoch = 1
        asyncio.run(router.awrap_model_call(self._request(), handler))
        self.assertEqual(provider.leases, 1)
        self.assertEqual(seen, ["pinned", "model-1"])

    def test_switch_before_first_call_drives_new_model(self) -> None:
        from langchain.agents.middleware.types import ModelRequest

        # Specialist-style router (no preselected selection) where the provider
        # is switched AFTER the router is constructed but BEFORE its first call:
        # the epoch moves relative to the construction snapshot, so the first
        # lease must override the graph-bound model.
        provider = self.FakeProvider()
        router = ModelRouter(provider, role="test", selection=None)
        provider.epoch = 1
        seen: list[str] = []

        async def handler(request: ModelRequest[object]) -> object:
            seen.append(request.model.model)
            return SimpleNamespace(result=["ok"])

        asyncio.run(router.awrap_model_call(self._request(), handler))
        self.assertEqual(provider.leases, 1)
        self.assertEqual(seen, ["model-1"])

        asyncio.run(router.awrap_model_call(self._request(), handler))
        self.assertEqual(provider.leases, 1)
        self.assertEqual(seen, ["model-1", "model-1"])


class OpenCodeGoPayloadCacheTests(unittest.TestCase):
    """Wire-level prefix-cache breakpoints and orphan tool-result filtering.

    Breakpoints are stamped on the outbound payload (where assistant and tool
    blocks survive serialization); orphan tool results are skipped on the wire
    only, so framework history stays byte-stable and the gateway's prefix
    cache keeps hitting across turns.
    """

    def _payload(self, messages: list[object]) -> dict[str, object]:
        from z_apply_core.agents.model_provider import OpenCodeGoProvider

        provider = OpenCodeGoProvider(api_key="sk-test")
        selection = asyncio.run(provider.lease())
        return selection.llm._get_request_payload(messages)

    def test_stamps_breakpoints_on_system_tail_and_last_tool(self) -> None:
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )

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
        # Stable prefix: system breakpoint.
        self.assertEqual(messages[0]["content"][0]["cache_control"], marker)
        # Moving tail: last 2 user/assistant messages (assistant + latest user).
        self.assertEqual(messages[2]["content"][0]["cache_control"], marker)
        self.assertEqual(messages[4]["content"][0]["cache_control"], marker)
        # Last tool result.
        self.assertEqual(messages[3]["content"][0]["cache_control"], marker)
        # Untouched middle: the first user message carries no breakpoint.
        self.assertNotIn("cache_control", messages[1]["content"][0])

    def test_unsatisfied_tool_calls_pruned_from_assistant_message(self) -> None:
        # A parallel batch where one call produced no result (framework dropped
        # it mid-batch) must not reach the gateway: an assistant message whose
        # tool_call_ids lack following tool messages is rejected with HTTP 400.
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )

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
        # A tool_calls-only assistant whose results all vanished must not reach
        # the gateway as a message with neither content nor tool_calls (HTTP
        # 400 "content or tool_calls must be set").
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
        )

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
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )

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
        tool_messages = [
            message for message in payload["messages"] if message["role"] == "tool"
        ]
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
        self.assertEqual(messages[1]["content"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
