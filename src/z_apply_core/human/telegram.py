from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)


def _norm(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _option_signature(value: str) -> str:
    text = _norm(value)
    text = re.sub(r"\bto\b", "-", text)
    text = re.sub(r"\s*-\s*", "-", text)
    return re.sub(r"[^a-z0-9]+", "", text)


async def _shutdown_application(
    app: Application[Any, Any, Any, Any, Any, Any],
) -> None:
    with contextlib.suppress(Exception):
        if app.updater is not None and app.updater.running:
            await app.updater.stop()
    with contextlib.suppress(Exception):
        if app.running:
            await app.stop()
    with contextlib.suppress(Exception):
        await app.shutdown()


@dataclass(slots=True)
class PendingHumanRequest:
    request_id: str
    topic_id: int | None
    prompt_message_id: int
    future: asyncio.Future[str]
    options: list[str]


class TelegramHumanChannel:
    def __init__(self, *, token: str, chat_id: int | str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.bot = Bot(token=token)
        self._pending: dict[str, PendingHumanRequest] = {}
        self._pending_by_topic: dict[int | None, set[str]] = {}
        self._pending_by_message: dict[int, str] = {}
        self._active_topics: dict[str, int | None] = {}
        self._created_topic_ids: set[int] = set()
        self._app: Application[Any, Any, Any, Any, Any, Any] | None = None
        self._start_lock = asyncio.Lock()
        self._ask_lock = asyncio.Lock()

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
        async with self._ask_lock:
            return await self._ask_once(
                question=question,
                context=context,
                url=url,
                company=company,
                role=role,
                options=options,
                risk=risk,
                image_path=image_path,
            )

    async def _ask_once(
        self,
        *,
        question: str,
        context: str,
        url: str,
        company: str,
        role: str,
        options: list[str] | None,
        risk: str,
        image_path: str,
    ) -> str:
        if self._app is None:
            await self.start()
        topic_id = await self._get_or_create_topic(url=url, company=company, role=role)
        request_id = uuid.uuid4().hex[:10]
        option_list = [option.strip() for option in (options or []) if option and option.strip()]

        text = self._message_text(
            request_id=request_id,
            question=question,
            context=context,
            url=url,
            risk=risk,
            has_options=bool(option_list),
            has_other_pending=bool(self._pending_by_topic.get(topic_id)),
        )
        sent = await self.bot.send_message(
            chat_id=self.chat_id,
            message_thread_id=topic_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=self._build_option_markup(request_id, option_list),
        )

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = PendingHumanRequest(
            request_id=request_id,
            topic_id=topic_id,
            prompt_message_id=sent.message_id,
            future=future,
            options=option_list,
        )
        self._pending[request_id] = pending
        self._pending_by_message[sent.message_id] = request_id
        self._pending_by_topic.setdefault(topic_id, set()).add(request_id)
        if image_path:
            await self._send_request_image(
                image_path=image_path,
                topic_id=topic_id,
                reply_to_message_id=sent.message_id,
            )
        return await future

    async def confirm(
        self,
        *,
        question: str,
        context: str = "",
        url: str = "",
        company: str = "System",
        role: str = "Application",
    ) -> bool:
        answer = await self.ask(
            question=question,
            context=context,
            url=url,
            company=company,
            role=role,
            options=["Approve", "Reject"],
            risk="high",
        )
        return _option_signature(answer) == "approve"

    async def start(self) -> None:
        async with self._start_lock:
            if self._app is not None:
                return

            app = Application.builder().token(self.token).build()
            app.add_handler(CallbackQueryHandler(self._handle_callback, pattern=r"^hitl:"))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_reply))

            try:
                await app.initialize()
                await app.start()
                if app.updater is not None:
                    await app.updater.start_polling(drop_pending_updates=True)
            except Exception:
                await _shutdown_application(app)
                raise
            self._app = app
            logger.info("Telegram human channel listener started")

    async def stop(self) -> None:
        async with self._start_lock:
            await self._close_created_topics()
            app = self._app
            if app is None:
                return
            self._app = None
            await _shutdown_application(app)
            logger.info("Telegram human channel listener stopped")

    async def resolve(self, request_id: str, answer: str) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is None:
            logger.warning("Ignoring answer for unknown human request: %s", request_id)
            return

        self._pending_by_message.pop(pending.prompt_message_id, None)
        topic_requests = self._pending_by_topic.get(pending.topic_id)
        if topic_requests is not None:
            topic_requests.discard(request_id)
            if not topic_requests:
                self._pending_by_topic.pop(pending.topic_id, None)

        if pending.options:
            try:
                await self.bot.edit_message_reply_markup(
                    chat_id=self.chat_id,
                    message_id=pending.prompt_message_id,
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to clear Telegram option buttons")

        if not pending.future.done():
            pending.future.set_result(answer)

    async def _get_or_create_topic(self, *, url: str, company: str, role: str) -> int | None:
        topic_key = url or f"{company}:{role}"
        if topic_key in self._active_topics:
            return self._active_topics[topic_key]

        topic_name = f"{company} | {role}"[:128]
        try:
            topic = await self.bot.create_forum_topic(chat_id=self.chat_id, name=topic_name)
        except Exception as exc:
            logger.warning(
                "Could not create Telegram forum topic; falling back to group chat: %s",
                exc,
            )
            self._active_topics[topic_key] = None
            return None

        topic_id = topic.message_thread_id
        self._active_topics[topic_key] = topic_id
        self._created_topic_ids.add(topic_id)
        return topic_id

    async def _send_request_image(
        self,
        *,
        image_path: str,
        topic_id: int | None,
        reply_to_message_id: int,
    ) -> None:
        path = Path(image_path).expanduser().resolve()
        artifact_root = (Path.cwd() / ".z-apply" / "runs").resolve()
        if not path.is_file() or not path.is_relative_to(artifact_root):
            logger.warning("Ignoring unsafe or missing Telegram image path: %s", image_path)
            return
        try:
            with path.open("rb") as image:
                await self.bot.send_photo(
                    chat_id=self.chat_id,
                    message_thread_id=topic_id,
                    photo=image,
                    reply_to_message_id=reply_to_message_id,
                )
        except Exception:
            logger.exception("Failed to send Telegram request image: %s", path)

    async def _close_created_topics(self) -> None:
        for topic_id in tuple(self._created_topic_ids):
            try:
                await self.bot.close_forum_topic(
                    chat_id=self.chat_id,
                    message_thread_id=topic_id,
                )
            except Exception:
                logger.exception("Failed to close Telegram forum topic %s", topic_id)
        self._created_topic_ids.clear()
        self._active_topics.clear()

    def _message_text(
        self,
        *,
        request_id: str,
        question: str,
        context: str,
        url: str,
        risk: str,
        has_options: bool,
        has_other_pending: bool,
    ) -> str:
        suffix = ""
        if has_options:
            suffix = "\n\nChoose one option below or reply with one exact option."
        elif has_other_pending:
            suffix = "\n\nReply to this specific message so I can map your answer correctly."

        parts = [
            "<b>Z-Apply needs input</b>",
            f"<b>Request ID:</b> <code>{html.escape(request_id)}</code>",
            f"<b>Risk:</b> {html.escape(risk)}",
            html.escape(question) + html.escape(suffix),
        ]
        if context:
            parts.append(f"<b>Context:</b>\n{html.escape(context)}")
        if url:
            parts.append(f'<a href="{html.escape(url)}">Current page</a>')
        return "\n\n".join(parts)

    def _build_option_markup(
        self,
        request_id: str,
        options: list[str],
    ) -> InlineKeyboardMarkup | None:
        if not options:
            return None

        rows: list[list[InlineKeyboardButton]] = []
        current_row: list[InlineKeyboardButton] = []
        for index, option in enumerate(options):
            current_row.append(
                InlineKeyboardButton(
                    text=option[:64],
                    callback_data=f"hitl:{request_id}:{index}",
                )
            )
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)
        return InlineKeyboardMarkup(rows)

    def _resolve_request_for_message(self, message: Message) -> str | None:
        reply_to = getattr(message, "reply_to_message", None)
        if reply_to and reply_to.message_id in self._pending_by_message:
            return self._pending_by_message[reply_to.message_id]

        pending_ids = list(self._pending_by_topic.get(message.message_thread_id) or [])
        if len(pending_ids) == 1:
            return pending_ids[0]

        text = (message.text or "").strip()
        if text:
            match = re.search(r"\b([0-9a-f]{10})\b", text.lower())
            if match and match.group(1) in self._pending:
                return match.group(1)
        return None

    async def _handle_callback(
        self,
        update: Update,
        _context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if query is None or not isinstance(query.message, Message):
            return
        message = query.message
        if str(message.chat_id) != str(self.chat_id):
            return

        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[0] != "hitl":
            await query.answer()
            return

        request_id = parts[1]
        pending = self._pending.get(request_id)
        if pending is None:
            await query.answer("That request is already resolved.", show_alert=True)
            return

        try:
            option_index = int(parts[2])
            answer = pending.options[option_index]
        except (ValueError, IndexError):
            await query.answer("Invalid option.", show_alert=True)
            return

        await self.resolve(request_id, answer)
        await query.answer(f"Selected: {answer}")
        await message.reply_text("Answer received. Resuming agent.")

    async def _handle_reply(
        self,
        update: Update,
        _context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        if message is None:
            return
        if message.from_user is not None and message.from_user.is_bot:
            return
        if str(message.chat_id) != str(self.chat_id):
            return

        request_id = self._resolve_request_for_message(message)
        if request_id is None:
            pending_count = len(self._pending_by_topic.get(message.message_thread_id) or [])
            if pending_count > 1:
                await message.reply_text(
                    "Reply to the specific pending question message, or use its option buttons."
                )
            return

        pending = self._pending.get(request_id)
        if pending is None:
            return

        answer = (message.text or "").strip()
        if pending.options:
            mapped = self._map_text_to_option(answer, pending.options)
            if mapped is None:
                await message.reply_text(
                    "I could not map that reply to one of the allowed options. "
                    "Use the buttons or reply with one exact option."
                )
                return
            answer = mapped

        await self.resolve(request_id, answer)
        await message.reply_text("Answer received. Resuming agent.")

    def _map_text_to_option(self, answer: str, options: list[str]) -> str | None:
        signature = _option_signature(answer)
        if not signature:
            return None
        matches = [option for option in options if _option_signature(option) == signature]
        if len(matches) == 1:
            return matches[0]
        return None
