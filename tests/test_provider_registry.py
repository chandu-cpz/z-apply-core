from __future__ import annotations

import unittest
from unittest.mock import patch

from z_apply_core.agents.model_provider import (
    AgnesProvider,
    InferXProvider,
    default_provider_name,
    get_provider,
    list_providers,
)
from z_apply_core.config import Settings


class ProviderSelectionTests(unittest.TestCase):
    def patch_settings(self, **overrides: str) -> patch:
        values = {
            "MODEL_PROVIDER": "",
            "AGNES_API_KEY": "",
            "INFERX_API_KEY": "",
            "INFERX_MODEL": "deepseek-v4-flash-0731",
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

    def test_auto_detect_prefers_agnes_when_both_keys_set(self) -> None:
        with self.patch_settings(AGNES_API_KEY="sk-test", INFERX_API_KEY="ix-test"):
            provider = get_provider()

        self.assertIsInstance(provider, AgnesProvider)

    def test_auto_detect_falls_back_to_inferx_when_only_inferx_key(self) -> None:
        with self.patch_settings(INFERX_API_KEY="ix-test"):
            provider = get_provider()

        self.assertIsInstance(provider, InferXProvider)

    def test_missing_key_on_explicit_provider_falls_back(self) -> None:
        with self.patch_settings(MODEL_PROVIDER="inferx", AGNES_API_KEY="sk-test"):
            provider = get_provider()

        self.assertIsInstance(provider, AgnesProvider)

    def test_unknown_provider_name_warns_and_falls_back(self) -> None:
        with self.patch_settings(MODEL_PROVIDER="nope", AGNES_API_KEY="sk-test"):
            provider = get_provider()

        self.assertIsInstance(provider, AgnesProvider)

    def test_nim_requires_router(self) -> None:
        with self.patch_settings(), self.assertRaises(ValueError):
            get_provider(provider_name="nim")
        with self.patch_settings(MODEL_PROVIDER="nim"), self.assertRaises(ValueError):
            get_provider()

    def test_no_provider_configured_raises(self) -> None:
        with self.patch_settings(), self.assertRaises(ValueError) as ctx:
            get_provider()

        self.assertIn("MODEL_PROVIDER", str(ctx.exception))

    def test_registry_lists_providers_in_detection_order(self) -> None:
        names = [spec.name for spec in list_providers()]

        self.assertEqual(names, ["agnes", "inferx", "nim"])

    def test_default_provider_name_reflects_env(self) -> None:
        with self.patch_settings(MODEL_PROVIDER="inferx"):
            self.assertEqual(default_provider_name(), "inferx")
        with self.patch_settings(AGNES_API_KEY="sk-test", INFERX_API_KEY="ix-test"):
            self.assertEqual(default_provider_name(), "agnes")

    def test_inferx_provider_default_model(self) -> None:
        provider = InferXProvider(api_key="ix-test")

        self.assertEqual(provider._model, "deepseek-v4-flash-0731")
        self.assertEqual(provider.BASE_URL, "https://model.inferx.net/endpoints/v1")


if __name__ == "__main__":
    unittest.main()
