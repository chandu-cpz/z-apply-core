from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from z_apply_core.human import sanitize
from z_apply_core.human.sanitize import (
    _NEUTRAL_PLACEHOLDER,
    default_owner_values,
    sanitize_human_text,
)


class SanitizeHumanTextTests(unittest.TestCase):
    def test_known_owner_value_is_substituted(self) -> None:
        self.assertEqual(
            sanitize_human_text(
                "confirm your email <secret>DEFAULT_USERNAME</secret>",
                known_values={"DEFAULT_USERNAME": "user@example.test"},
            ),
            "confirm your email user@example.test",
        )

    def test_no_secret_token_survives_for_unknown_name(self) -> None:
        settings = SimpleNamespace(default_username="", default_password="")
        with patch("z_apply_core.human.sanitize.load_settings", return_value=settings):
            out = sanitize_human_text("please provide <secret>BOGUS</secret>")
        self.assertNotIn("<secret>", out)
        self.assertNotIn("</secret>", out)
        self.assertNotIn("BOGUS", out)
        self.assertEqual(out, f"please provide {_NEUTRAL_PLACEHOLDER}")

    def test_multiple_tokens_handled(self) -> None:
        known = {"A": "a@x.test", "B": "b@x.test"}
        out = sanitize_human_text("<secret>A</secret> and <secret>B</secret>", known_values=known)
        self.assertEqual(out, "a@x.test and b@x.test")

    def test_empty_text_is_passthrough(self) -> None:
        self.assertEqual(sanitize_human_text(""), "")
        self.assertEqual(sanitize_human_text("just prose"), "just prose")

    def test_default_owner_values_uses_settings(self) -> None:
        settings = SimpleNamespace(default_username="name@owner.test", default_password="pw")
        with patch("z_apply_core.human.sanitize.load_settings", return_value=settings):
            self.assertEqual(
                sanitize_human_text("<secret>DEFAULT_USERNAME</secret>"),
                "name@owner.test",
            )

    def test_default_owner_values_filters_empty(self) -> None:
        settings = SimpleNamespace(default_username="", default_password="")
        with patch("z_apply_core.human.sanitize.load_settings", return_value=settings):
            self.assertEqual(default_owner_values(), {})

    def test_whitespace_inside_name_is_collapsed(self) -> None:
        out = sanitize_human_text(
            "<secret> DEFAULT_USERNAME </secret>",
            known_values={"DEFAULT_USERNAME": "owner@x.test"},
        )
        self.assertEqual(out, "owner@x.test")

    def test_module_exposes_expected_symbols(self) -> None:
        self.assertTrue(callable(sanitize.default_owner_values))
        self.assertTrue(callable(sanitize.sanitize_human_text))


if __name__ == "__main__":
    unittest.main()
