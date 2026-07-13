from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from z_apply_core.browser_config import build_browser_config


class BrowserConfigTests(unittest.TestCase):
    def test_default_credentials_are_registered_as_browser_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                default_username="user@example.test",
                default_password="secret",
                camoufox_browser="official/150.0.2-alpha.26",
                simplify_addon_path=Path(directory),
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

    def test_simplify_addon_is_loaded_in_every_browser_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            addon = Path(directory) / "simplify"
            addon.mkdir()
            settings = SimpleNamespace(
                default_username="",
                default_password="",
                camoufox_browser="official/150.0.2-alpha.26",
                simplify_addon_path=addon,
            )
            with patch("z_apply_core.browser_config.load_settings", return_value=settings):
                config = build_browser_config()

        self.assertEqual(
            config["browser"]["camoufoxOptions"]["addons"],
            [str(addon.resolve())],
        )
        self.assertEqual(
            config["browser"]["camoufoxOptions"]["browser"],
            "official/150.0.2-alpha.26",
        )


if __name__ == "__main__":
    unittest.main()
