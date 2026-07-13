from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from nim_router.config import RouterConfig

from z_apply_core import graph
from z_apply_core.model_policy import BANNED_MODEL_IDS_UNDER_30B


class ModelPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_excludes_models_observed_failing_native_tool_execution(self) -> None:
        excluded = set(BANNED_MODEL_IDS_UNDER_30B)

        self.assertIn("bytedance/seed-oss-36b-instruct", excluded)
        self.assertIn("google/gemma-4-31b-it", excluded)
        self.assertIn("meta/llama-3.1-70b-instruct", excluded)
        self.assertIn("moonshotai/kimi-k2-instruct-0905", excluded)
        self.assertIn("nvidia/llama-3.3-nemotron-super-49b-v1", excluded)
        self.assertIn("nvidia/nemotron-3-super-120b-a12b", excluded)
        self.assertIn("openai/gpt-oss-120b", excluded)
        self.assertIn("meta/llama-3.3-70b-instruct", excluded)
        self.assertIn("qwen/qwen3-next-80b-a3b-instruct", excluded)
        self.assertIn("mistralai/mistral-medium-3.5-128b", excluded)
        self.assertIn("deepseek-ai/deepseek-v3.1-terminus", excluded)
        self.assertIn("deepseek-ai/deepseek-v3.2", excluded)
        self.assertIn("moonshotai/kimi-k2-instruct", excluded)
        self.assertIn("moonshotai/kimi-k2-thinking", excluded)
        self.assertNotIn("qwen/qwen3.5-122b-a10b", excluded)
        self.assertNotIn("qwen/qwen3-235b-a22b", excluded)
        self.assertNotIn("stepfun-ai/step-3.5-flash", excluded)
        self.assertNotIn("stepfun-ai/step-3.7-flash", excluded)
        self.assertIn("z-ai/glm-5.1", excluded)

    async def test_run_job_adds_core_bans_to_environment_exclusions(self) -> None:
        configured = RouterConfig(
            excluded_models=[
                "user/excluded-model",
                "meta/llama-3.2-3b-instruct",
            ]
        )
        compiled_graph = MagicMock()
        compiled_graph.astream_events.return_value = object()

        with (
            patch.object(graph, "build_graph", return_value=compiled_graph),
            patch.object(graph.RouterConfig, "from_env", return_value=configured),
            patch.object(graph, "NimRouter") as router_type,
            patch.object(
                graph,
                "consume_v3_events",
                AsyncMock(return_value=SimpleNamespace(output={})),
            ),
        ):
            await graph.run_job("https://example.test/job", task="prepare")

        router_type.assert_called_once_with(config=configured)
        self.assertEqual(configured.excluded_models[0], "user/excluded-model")
        self.assertEqual(
            configured.excluded_models.count("meta/llama-3.2-3b-instruct"),
            1,
        )
        self.assertTrue(set(BANNED_MODEL_IDS_UNDER_30B) <= set(configured.excluded_models))
        self.assertIn("openai/gpt-oss-20b", configured.excluded_models)
        self.assertIn("openai/gpt-oss-120b", configured.excluded_models)
        self.assertIn("nvidia/nemotron-3-nano-30b-a3b", configured.excluded_models)
        self.assertEqual(configured.exploration_interval_seconds, 300.0)


if __name__ == "__main__":
    unittest.main()
