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

    def test_owner_email_is_seeded_as_non_lookupable_redact_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                default_username="user@example.test",
                default_password="secret",
                camoufox_browser="official/150.0.2-alpha.26",
                simplify_addon_path=Path(directory),
            )

            with patch("z_apply_core.browser_config.load_settings", return_value=settings):
                config = build_browser_config()

        self.assertEqual(config["redact_values"], {"DEFAULT_USERNAME": "user@example.test"})

    def test_no_redact_values_when_owner_identity_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                default_username="",
                default_password="",
                camoufox_browser="official/150.0.2-alpha.26",
                simplify_addon_path=Path(directory),
            )
            with patch("z_apply_core.browser_config.load_settings", return_value=settings):
                config = build_browser_config()

        self.assertNotIn("redact_values", config)

    def test_simplify_addon_is_not_installed_per_session(self) -> None:
        """Simplify ships in the profile as a baked sideload, never a temp install."""
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                default_username="",
                default_password="",
                camoufox_browser="official/150.0.2-alpha.26",
                simplify_addon_path=Path(directory) / "simplify",
            )
            with patch("z_apply_core.browser_config.load_settings", return_value=settings):
                config = build_browser_config()

        self.assertNotIn("addons", config["browser"]["camoufoxOptions"])
        self.assertEqual(
            config["browser"]["camoufoxOptions"]["browser"],
            "official/150.0.2-alpha.26",
        )
        self.assertEqual(config["timeouts"]["navigation"], 120_000)

    def test_profile_dir_override_points_browser_at_a_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            addon = Path(directory) / "simplify"
            addon.mkdir()
            slot = Path(directory) / "slot-1"
            settings = SimpleNamespace(
                default_username="",
                default_password="",
                camoufox_browser="",
                simplify_addon_path=addon,
            )
            with patch("z_apply_core.browser_config.load_settings", return_value=settings):
                config = build_browser_config("run-42", profile_dir=slot)

            self.assertEqual(config["browser"]["userDataDir"], str(slot))
            self.assertTrue(slot.is_dir())


if __name__ == "__main__":
    unittest.main()
