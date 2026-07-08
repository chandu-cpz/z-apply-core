from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from z_apply_core.config import Settings, load_settings
from z_apply_core.human.factory import make_configured_human_channel
from z_apply_core.human.telegram import TelegramHumanChannel
from z_apply_core.human.tools import make_human_tools


class FakeHumanChannel:
    async def ask(
        self,
        *,
        question: str,
        context: str = "",
        url: str = "",
        company: str = "System",
        role: str = "Application",
        options: list[str] | None = None,
        risk: str = "medium",
    ) -> str:
        return options[0] if options else f"{company}:{role}:{question}:{context}:{url}:{risk}"

    async def confirm(
        self,
        *,
        question: str,
        context: str = "",
        url: str = "",
        company: str = "System",
        role: str = "Application",
    ) -> bool:
        return bool(question and context and url and company and role)


class FakeSentMessage:
    message_id = 123


class FakeBot:
    async def send_message(self, **_kwargs: Any) -> FakeSentMessage:
        return FakeSentMessage()

    async def edit_message_reply_markup(self, **_kwargs: Any) -> None:
        return None

    async def create_forum_topic(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(message_thread_id=777)


class SettingsTests(unittest.TestCase):
    def test_settings_flags_are_based_on_present_values(self) -> None:
        settings = Settings(
            DEFAULT_USERNAME="user@example.com",
            DEFAULT_PASSWORD="secret",
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_GROUP_CHAT_ID="-100",
        )

        self.assertTrue(settings.has_default_credentials)
        self.assertTrue(settings.has_telegram)

    def test_settings_load_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEFAULT_USERNAME": "user@example.com",
                "DEFAULT_PASSWORD": "secret",
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_GROUP_CHAT_ID": "-100",
            },
        ):
            load_settings.cache_clear()
            settings = load_settings()

        self.assertTrue(settings.has_default_credentials)
        self.assertTrue(settings.has_telegram)

    def test_configured_human_channel_uses_telegram_when_available(self) -> None:
        settings = Settings(
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_GROUP_CHAT_ID="-100",
        )

        channel = make_configured_human_channel(settings)

        self.assertIsInstance(channel, TelegramHumanChannel)


class HumanToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_human_tools_delegate_to_channel(self) -> None:
        ask_human, request_submit_approval = make_human_tools(FakeHumanChannel())

        answer = await ask_human.ainvoke(
            {
                "question": "Pick one",
                "company_name": "Acme",
                "role_name": "Engineer",
                "options": ["Yes", "No"],
            }
        )
        approval = await request_submit_approval.ainvoke(
            {
                "final_review": "Ready",
                "url": "https://example.test",
                "company_name": "Acme",
                "role_name": "Engineer",
            }
        )

        self.assertEqual(answer, {"human_answer": "Yes"})
        self.assertEqual(approval, {"submit_approval": "approved"})


class TelegramHumanChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_ask_waits_for_pending_future_resolution(self) -> None:
        channel = TelegramHumanChannel(token="token", chat_id="-100")
        channel.bot = FakeBot()  # type: ignore[assignment]
        channel._app = SimpleNamespace()

        task = asyncio.create_task(
            channel.ask(
                question="Need value",
                company="Acme",
                role="Engineer",
                options=["Alpha", "Beta"],
            )
        )
        await asyncio.sleep(0)

        self.assertEqual(len(channel._pending), 1)
        request_id = next(iter(channel._pending))
        await channel.resolve(request_id, "Alpha")

        self.assertEqual(await task, "Alpha")
        self.assertFalse(channel._pending)


if __name__ == "__main__":
    unittest.main()
