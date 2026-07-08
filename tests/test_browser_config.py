from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from z_apply_core.browser_config import build_browser_config


class BrowserConfigTests(unittest.TestCase):
    def test_default_credentials_are_registered_as_browser_secrets(self) -> None:
        settings = SimpleNamespace(
            default_username="user@example.test",
            default_password="secret",
        )

        with patch("z_apply_core.browser_config.load_settings", return_value=settings):
            config = build_browser_config()

        self.assertEqual(
            config["secrets"],
            {
                "DEFAULT_USERNAME": "user@example.test",
                "DEFAULT_PASSWORD": "secret",
            },
        )


if __name__ == "__main__":
    unittest.main()
