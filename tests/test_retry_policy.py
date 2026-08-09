import unittest

from z_apply_core.agents.model_provider import OpenCodeGoProvider
from z_apply_core.agents.retry_policy import (
    is_instant_retry_provider,
    model_retry_middleware,
)


class TestRetryPolicy(unittest.TestCase):
    def test_default_backoff_unchanged(self) -> None:
        network, inner = model_retry_middleware()
        # Inner layer keeps the provider's paced backoff.
        self.assertEqual(inner.max_retries, 8)
        self.assertEqual(inner.initial_delay, 1.0)
        self.assertEqual(inner.backoff_factor, 1.7)
        self.assertEqual(inner.max_delay, 12.0)
        self.assertTrue(inner.jitter)
        # Outer layer pauses long on genuine network loss.
        self.assertEqual(network.max_retries, 40)
        self.assertEqual(network.initial_delay, 5.0)
        self.assertEqual(network.max_delay, 60.0)
        self.assertTrue(network.jitter)

    def test_opencodego_retries_instantly_without_delay(self) -> None:
        provider = OpenCodeGoProvider(api_key="test-key")
        self.assertTrue(is_instant_retry_provider(provider))
        network, inner = model_retry_middleware(provider)
        self.assertEqual(inner.max_retries, 8)
        self.assertEqual(inner.initial_delay, 0.0)
        self.assertEqual(inner.backoff_factor, 0.0)
        self.assertEqual(inner.max_delay, 0.0)
        self.assertFalse(inner.jitter)
        # Network-loss layer still waits even on the instant-retry provider.
        self.assertEqual(network.initial_delay, 5.0)

    def test_none_provider_keeps_default_backoff(self) -> None:
        self.assertFalse(is_instant_retry_provider(None))
        network, inner = model_retry_middleware(None)
        self.assertEqual(inner.initial_delay, 1.0)
        self.assertTrue(inner.jitter)
        self.assertEqual(network.max_retries, 40)


if __name__ == "__main__":
    unittest.main()
