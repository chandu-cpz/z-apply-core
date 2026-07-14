from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from z_apply_core.config import Settings, load_settings
from z_apply_core.human.factory import make_configured_human_channel
from z_apply_core.human.telegram import TelegramHumanChannel
from z_apply_core.human.tools import make_human_tools, make_manual_auth_tool


class FakeHumanChannel:
    async def send_artifact(self, *, path: str, caption: str) -> None:
        del path, caption

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
        image_path: str = "",
    ) -> str:
        return (
            options[0]
            if options
            else (f"{company}:{role}:{question}:{context}:{url}:{risk}:{image_path}")
        )

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
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.photos: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.closed_topics: list[int] = []
        self.created_topics: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> FakeSentMessage:
        self.messages.append(kwargs)
        return FakeSentMessage()

    async def send_photo(self, **kwargs: Any) -> None:
        self.photos.append(kwargs)

    async def send_document(self, **kwargs: Any) -> None:
        self.documents.append(kwargs)

    async def edit_message_reply_markup(self, **_kwargs: Any) -> None:
        return None

    async def create_forum_topic(self, **kwargs: Any) -> Any:
        self.created_topics.append(kwargs)
        return SimpleNamespace(message_thread_id=777)

    async def close_forum_topic(self, **kwargs: Any) -> None:
        self.closed_topics.append(kwargs["message_thread_id"])


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
    async def test_manual_auth_handoff_never_requests_credentials(self) -> None:
        channel = SimpleNamespace(ask=AsyncMock(return_value="Done"))
        manual_auth = make_manual_auth_tool(
            channel,
            human_challenge_image_path="/runtime/captcha.png",
        )

        result = await manual_auth.ainvoke(
            {
                "challenge_summary": "Workday sign-in needs manual completion.",
                "url": "https://example.test/signin",
            }
        )

        request = channel.ask.await_args.kwargs
        self.assertNotIn("password", request["question"].casefold())
        self.assertNotIn("credential", request["question"].casefold())
        self.assertEqual(request["options"], ["Done", "Cannot complete"])
        self.assertEqual(request["image_path"], "/runtime/captcha.png")
        self.assertEqual(result, {"manual_auth": "done"})

    async def test_human_tools_delegate_to_channel(self) -> None:
        ask_human, request_submit_approval = make_human_tools(FakeHumanChannel())

        answer = await ask_human.ainvoke(
            {
                "question": "Pick one",
                "reason": "missing_candidate_fact",
                "field_label": "Choice",
                "field_evidence": "empty field",
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

        self.assertEqual(
            answer,
            {"human_answer": "Yes", "candidate_memory_stored": "false"},
        )
        self.assertEqual(approval, {"submit_approval": "approved"})

    async def test_human_challenge_uses_runtime_owned_image_path(self) -> None:
        capture = AsyncMock(return_value=Path("/runtime/captcha.png"))
        ask_human, _ = make_human_tools(
            FakeHumanChannel(),
            capture_human_challenge=capture,
        )

        answer = await ask_human.ainvoke(
            {
                "question": "Enter CAPTCHA",
                "reason": "human_challenge",
                "challenge_target": "e42",
            }
        )

        capture.assert_awaited_once_with("e42")
        self.assertTrue(answer["human_answer"].endswith(":/runtime/captcha.png"))

    async def test_review_artifact_is_published_before_submit_approval(self) -> None:
        before_approval = AsyncMock()
        _, request_submit_approval = make_human_tools(
            FakeHumanChannel(),
            before_submit_approval=before_approval,
        )

        result = await request_submit_approval.ainvoke(
            {"final_review": "Ready", "url": "https://example.test"}
        )

        before_approval.assert_awaited_once()
        self.assertEqual(result, {"submit_approval": "approved"})


class TelegramHumanChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_artifacts_are_sent_in_the_bound_topic(self) -> None:
        channel = TelegramHumanChannel(token="token", chat_id="-100")
        bot = FakeBot()
        channel.bot = bot  # type: ignore[assignment]
        channel._app = SimpleNamespace()
        channel.bind_run(run_id="run-artifacts", url="https://jobs.example.test/123")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / ".z-apply" / "runs" / "run-artifacts"
            artifact_root.mkdir(parents=True)
            pdf = artifact_root / "application-review.pdf"
            png = artifact_root / "submission-confirmation.png"
            pdf.write_bytes(b"pdf")
            png.write_bytes(b"png")
            with patch("z_apply_core.human.telegram.CORE_ROOT", root):
                await channel.send_artifact(path=str(pdf), caption="Review")
                await channel.send_artifact(path=str(png), caption="Submitted")

        self.assertEqual(len(bot.created_topics), 1)
        self.assertEqual(bot.documents[0]["message_thread_id"], 777)
        self.assertEqual(bot.photos[0]["message_thread_id"], 777)

    async def test_bound_run_reuses_one_topic_across_different_question_metadata(
        self,
    ) -> None:
        channel = TelegramHumanChannel(token="token", chat_id="-100")
        bot = FakeBot()
        channel.bot = bot  # type: ignore[assignment]
        channel._app = SimpleNamespace()
        channel.bind_run(
            run_id="run-123456789",
            url="https://jobs.example.test/123",
        )

        first = asyncio.create_task(
            channel.ask(
                question="CAPTCHA",
                url="https://jobs.example.test/123/apply",
                company="Acme",
                role="Engineer",
            )
        )
        await asyncio.sleep(0)
        await channel.resolve(next(iter(channel._pending)), "1234")
        self.assertEqual(await first, "1234")

        second = asyncio.create_task(
            channel.ask(
                question="Submit?",
                url="",
                company="System",
                role="Application",
            )
        )
        await asyncio.sleep(0)
        await channel.resolve(next(iter(channel._pending)), "Approve")
        self.assertEqual(await second, "Approve")

        self.assertEqual(len(bot.created_topics), 1)
        self.assertEqual(
            {message["message_thread_id"] for message in bot.messages},
            {777},
        )

    async def test_ask_waits_for_pending_future_resolution(self) -> None:
        channel = TelegramHumanChannel(token="token", chat_id="-100")
        bot = FakeBot()
        channel.bot = bot  # type: ignore[assignment]
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
        markup = bot.messages[0]["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels, ["Alpha", "Beta"])

    async def test_questions_for_concurrent_runs_can_wait_independently(self) -> None:
        channel = TelegramHumanChannel(token="token", chat_id="-100")
        channel.bot = FakeBot()  # type: ignore[assignment]
        channel._app = SimpleNamespace()

        first = asyncio.create_task(channel.ask(question="First"))
        second = asyncio.create_task(channel.ask(question="Second"))
        await asyncio.sleep(0)
        self.assertEqual(len(channel._pending), 2)

        first_id = next(iter(channel._pending))
        await channel.resolve(first_id, "one")
        self.assertEqual(await first, "one")
        self.assertEqual(len(channel._pending), 1)

        second_id = next(iter(channel._pending))
        await channel.resolve(second_id, "two")
        self.assertEqual(await second, "two")

    async def test_captcha_image_is_attached_and_created_topics_are_closed(self) -> None:
        channel = TelegramHumanChannel(token="token", chat_id="-100")
        bot = FakeBot()
        channel.bot = bot  # type: ignore[assignment]
        channel._app = SimpleNamespace()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / ".z-apply" / "runs" / "run-1" / "captcha.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            with patch("z_apply_core.human.telegram.CORE_ROOT", root):
                task = asyncio.create_task(
                    channel.ask(question="Enter CAPTCHA", image_path=str(image_path))
                )
                await asyncio.sleep(0)
                request_id = next(iter(channel._pending))
                await channel.resolve(request_id, "1234")
                self.assertEqual(await task, "1234")

        self.assertEqual(len(bot.photos), 1)
        await channel._close_created_topics()
        self.assertEqual(bot.closed_topics, [777])


if __name__ == "__main__":
    unittest.main()
