from __future__ import annotations

import asyncio
import html
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal, cast

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from z_apply_core.human.sanitize import sanitize_human_text
from z_apply_core.paths import runs_root
from z_apply_core.teardown import abest_effort

logger = logging.getLogger(__name__)

# PTB's HTTPXRequest defaults to 5s for read/write/connect and 1s for pool
# timeouts; on a flaky network a single slow Telegram call raises
# ``TimedOut: Timed out`` after ~5s and, unhandled, kills the asking agent.
# These values give the bot API generous headroom so brief network stalls do
# not fail a human question. ``get_updates_*`` apply to the polling loop.
_TELEGRAM_NETWORK_TIMEOUTS: dict[str, float] = {
    "connect_timeout": 30.0,
    "read_timeout": 60.0,
    "write_timeout": 60.0,
    "pool_timeout": 10.0,
    "get_updates_connect_timeout": 30.0,
    "get_updates_read_timeout": 30.0,
    "get_updates_write_timeout": 30.0,
    "get_updates_pool_timeout": 10.0,
}

_NETWORK_RETRY_ATTEMPTS = 3
_NETWORK_RETRY_BACKOFF_SECONDS = 1.5


async def _retry_network_call(
    factory: Callable[[], Awaitable[Any]],
    *,
    attempts: int = _NETWORK_RETRY_ATTEMPTS,
    backoff: float = _NETWORK_RETRY_BACKOFF_SECONDS,
    description: str = "Telegram request",
) -> Any:
    """Run one Telegram API call, retrying transient network failures.

    ``TimedOut`` and plain ``NetworkError`` are transport-level flakes (PTB
    cannot tell whether the request landed), so they are retried with short
    backoff like the official ``network_retry_loop`` does for polling. In PTB
    22.x ``BadRequest`` subclasses ``NetworkError`` even though it is a
    definitive API rejection, so it is never retried. Everything else
    propagates immediately.
    """
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await factory()
        except BadRequest:
            raise
        except NetworkError as exc:
            last_error = exc
            if attempt < attempts - 1:
                delay = backoff * (attempt + 1)
                logger.warning(
                    "%s failed with %s (attempt %s/%s); retrying in %.1fs",
                    description,
                    exc,
                    attempt + 1,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


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
    async def stop_updater() -> None:
        if app.updater is not None and app.updater.running:
            await app.updater.stop()

    async def stop_app() -> None:
        if app.running:
            await app.stop()

    await abest_effort("telegram updater stop", stop_updater)
    await abest_effort("telegram app stop", stop_app)
    await abest_effort("telegram app shutdown", app.shutdown)


@dataclass(slots=True)
class PendingHumanRequest:
    request_id: str
    topic_id: int | None
    prompt_message_id: int
    future: asyncio.Future[str]
    options: list[str]
    field_label: str = ""


class TelegramHumanChannel:
    def __init__(
        self,
        *,
        token: str,
        chat_id: int | str,
        proxy: str = "",
        bot_api_base: str = "",
        artifact_root: Path | None = None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self._proxy = proxy
        self._bot_api_base = bot_api_base
        # Containment root for sendable artifacts; defaults to the runs tree.
        # Injectable so tests can point it at a temp directory instead of
        # patching module globals.
        self._artifact_root = artifact_root
        self.bot = Bot(
            token=token,
            base_url=bot_api_base or "https://api.telegram.org/bot",
        )
        self._pending: dict[str, PendingHumanRequest] = {}
        self._pending_by_topic: dict[int | None, set[str]] = {}
        self._pending_by_message: dict[int, str] = {}
        self._active_topics: dict[str, int | None] = {}
        self._created_topic_ids: set[int] = set()
        self._run_topic_key: str | None = None
        self._run_topic_name: str | None = None
        self._app: Application[Any, Any, Any, Any, Any, Any] | None = None
        self._start_lock = asyncio.Lock()
        self._healthy: bool | None = None

    def bind_run(
        self,
        *,
        run_id: str,
        url: str,
        company: str = "Z-Apply",
        role: str = "Job application",
    ) -> None:
        """Bind every human interaction for one runtime to one forum topic."""
        if self._pending or self._created_topic_ids:
            raise RuntimeError("Cannot rebind an active Telegram human channel.")
        identity = run_id or url
        if not identity:
            raise ValueError("Telegram run binding requires a run ID or job URL.")
        self._run_topic_key = f"run:{identity}"
        self._run_topic_name = f"{company} | {role} | {identity[:8]}"[:128]

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
        field_label: str = "",
        reason: str = "",
    ) -> str:
        return await self.ask_with_id(
            request_id=uuid.uuid4().hex[:10],
            question=question,
            context=context,
            url=url,
            company=company,
            role=role,
            options=options,
            risk=risk,
            image_path=image_path,
        )

    async def ask_with_id(
        self,
        *,
        request_id: str,
        question: str,
        context: str = "",
        url: str = "",
        company: str = "System",
        role: str = "Application",
        options: list[str] | None = None,
        risk: str = "medium",
        image_path: str = "",
        field_label: str = "",
        reason: str = "",
    ) -> str:
        return await self._ask_once(
            request_id=request_id,
            question=question,
            context=context,
            url=url,
            company=company,
            role=role,
            options=options,
            risk=risk,
            image_path=image_path,
        )

    async def send_artifact(self, *, path: str, caption: str) -> None:
        """Post a run-owned image or PDF into the bound application topic."""
        await self.send_artifact_for(
            path=path,
            caption=caption,
            url="",
            company="Z-Apply",
            role="Job application",
        )

    async def send_artifact_for(
        self,
        *,
        path: str,
        caption: str,
        url: str,
        company: str,
        role: str,
    ) -> None:
        """Post an artifact into the topic associated with an application URL."""
        if self._app is None:
            await self.start()
        topic_id = await self._get_or_create_topic(
            url=url,
            company=company,
            role=role,
        )
        artifact = self._safe_artifact_path(path)
        if artifact is None:
            logger.warning("Ignoring unsafe or missing Telegram artifact: %s", path)
            return
        sanitized_caption = sanitize_human_text(caption)

        try:
            with artifact.open("rb") as content:
                kind: Literal["photo", "document"] = (
                    "document" if artifact.suffix.casefold() == ".pdf" else "photo"
                )
                await self._send_with_retry(
                    artifact=artifact,
                    topic_id=topic_id,
                    kind=kind,
                    content=content,
                    caption=sanitized_caption,
                )
        except Exception:
            logger.exception("Failed to send Telegram artifact: %s", artifact)

    async def _send_with_retry(
        self,
        *,
        artifact: Path,
        topic_id: int | None,
        kind: Literal["photo", "document"],
        content: IO[bytes],
        caption: str,
    ) -> None:
        """Send a photo or document artifact with generous timeouts and one retry."""

        async def send_once() -> None:
            if kind == "photo":
                await self.bot.send_photo(
                    chat_id=self.chat_id,
                    message_thread_id=topic_id,
                    photo=content,
                    caption=caption,
                    read_timeout=120,
                    write_timeout=120,
                )
            else:
                await self.bot.send_document(
                    chat_id=self.chat_id,
                    message_thread_id=topic_id,
                    document=content,
                    caption=caption,
                    read_timeout=120,
                    write_timeout=120,
                )

        try:
            await send_once()
        except Exception:
            logger.warning(
                "Telegram %s send failed for %s; retrying once.",
                kind,
                artifact,
            )
            await send_once()

    async def _ask_once(
        self,
        *,
        request_id: str,
        question: str,
        context: str,
        url: str,
        company: str,
        role: str,
        options: list[str] | None,
        risk: str,
        image_path: str,
        field_label: str = "",
        reason: str = "",
    ) -> str:
        if self._app is None:
            await self.start()
        if not await self._ensure_healthy():
            raise RuntimeError(
                "Telegram Bot API is unreachable from this machine; the human "
                "question could not be delivered. Check network access to "
                "api.telegram.org and retry once the channel is reachable."
            )
        topic_id = await self._get_or_create_topic(url=url, company=company, role=role)
        option_list = [option.strip() for option in (options or []) if option and option.strip()]

        if field_label:
            for pending_req in self._pending.values():
                if pending_req.field_label == field_label:
                    logger.info(
                        "Reusing the pending Telegram question for field %r",
                        field_label,
                    )
                    return await pending_req.future

        text = self._message_text(
            request_id=request_id,
            question=question,
            context=context,
            url=url,
            risk=risk,
            has_options=bool(option_list),
            has_other_pending=bool(self._pending_by_topic.get(topic_id)),
        )
        sent = await _retry_network_call(
            lambda: self.bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=topic_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=self._build_option_markup(request_id, option_list),
            ),
            description="Telegram question send",
        )
        sent = cast(Message, sent)

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = PendingHumanRequest(
            request_id=request_id,
            topic_id=topic_id,
            prompt_message_id=sent.message_id,
            future=future,
            options=option_list,
            field_label=field_label,
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

    async def cancel_request(self, request_id: str) -> None:
        pending = self._pending.get(request_id)
        if pending is None:
            return
        if not pending.future.done():
            pending.future.cancel()
        await self.resolve(request_id, "Cancelled")

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

            builder = Application.builder().token(self.token)
            if self._bot_api_base:
                builder = builder.base_url(self._bot_api_base)
            if self._proxy:
                builder = builder.proxy(self._proxy)
            for timeout_name, timeout_value in _TELEGRAM_NETWORK_TIMEOUTS.items():
                builder = getattr(builder, timeout_name)(timeout_value)
            app = builder.build()
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
            self._healthy = await self._probe_health()
            if not self._healthy:
                logger.error(
                    "Telegram Bot API is UNREACHABLE (%s). Human questions will "
                    "fail fast with a visible error until Telegram is reachable "
                    "again; the run continues without them.",
                    self.chat_id,
                )

    async def _probe_health(self) -> bool:
        """Probe the Bot API with a short timeout; no retries."""
        if self._app is None:
            return False
        try:
            await self.bot.get_me(read_timeout=8, connect_timeout=8, write_timeout=8)
            return True
        except Exception as exc:  # noqa: BLE001 - any transport failure means unhealthy
            logger.warning("Telegram health probe failed: %s", exc)
            return False

    async def _ensure_healthy(self) -> bool:
        """Return True when the channel can deliver; re-probe after failures.

        A downed Telegram API must fail the ask fast and visibly instead of
        hanging the run inside silent send retries.
        """
        if self._healthy is not False:
            return True
        self._healthy = await self._probe_health()
        return bool(self._healthy)

    async def stop(self) -> None:
        async with self._start_lock:
            await self._delete_created_topics()
            app = self._app
            if app is None:
                return
            self._app = None
            self._healthy = None
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
        topic_key = self._run_topic_key or url or f"{company}:{role}"
        if topic_key in self._active_topics:
            return self._active_topics[topic_key]

        topic_name = self._run_topic_name or f"{company} | {role}"[:128]
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
        path = self._safe_artifact_path(image_path)
        if path is None:
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

    def _safe_artifact_path(self, path: str) -> Path | None:
        artifact = Path(path).expanduser().resolve()
        artifact_root = (self._artifact_root or runs_root()).resolve()
        if not artifact.is_file() or not artifact.is_relative_to(artifact_root):
            return None
        if artifact.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
            return None
        return artifact

    async def _delete_created_topics(self) -> None:
        for topic_id in tuple(self._created_topic_ids):
            try:
                await self.bot.delete_forum_topic(
                    chat_id=self.chat_id,
                    message_thread_id=topic_id,
                )
            except Exception:
                logger.exception("Failed to delete Telegram forum topic %s", topic_id)
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
            html.escape(sanitize_human_text(question)) + html.escape(suffix),
        ]
        if context:
            parts.append(f"<b>Context:</b>\n{html.escape(sanitize_human_text(context))}")
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
