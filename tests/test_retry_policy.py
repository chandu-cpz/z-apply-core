import unittest

from z_apply_core.agents.model_provider import OpenCodeGoProvider
from z_apply_core.agents.retry_policy import (
    is_instant_retry_provider,
    model_retry_middleware,
)


class TestRetryPolicy(unittest.TestCase):
    def test_default_backoff_unchanged(self) -> None:
        mw = model_retry_middleware()
        self.assertEqual(mw.max_retries, 8)
        self.assertEqual(mw.initial_delay, 1.0)
        self.assertEqual(mw.backoff_factor, 1.7)
        self.assertEqual(mw.max_delay, 12.0)
        self.assertTrue(mw.jitter)

    def test_opencodego_retries_instantly_without_delay(self) -> None:
        provider = OpenCodeGoProvider(api_key="test-key")
        self.assertTrue(is_instant_retry_provider(provider))
        mw = model_retry_middleware(provider)
        self.assertEqual(mw.max_retries, 8)
        self.assertEqual(mw.initial_delay, 0.0)
        self.assertEqual(mw.backoff_factor, 0.0)
        self.assertEqual(mw.max_delay, 0.0)
        self.assertFalse(mw.jitter)

    def test_none_provider_keeps_default_backoff(self) -> None:
        self.assertFalse(is_instant_retry_provider(None))
        mw = model_retry_middleware(None)
        self.assertEqual(mw.initial_delay, 1.0)
        self.assertTrue(mw.jitter)


if __name__ == "__main__":
    unittest.main()
