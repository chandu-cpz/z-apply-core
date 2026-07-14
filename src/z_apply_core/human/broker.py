from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from z_apply_core.human.channel import HumanChannel


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    request_id: str
    run_id: str
    kind: str
    question: str
    context: str
    options: tuple[str, ...]
    risk: str
    allow_free_text: bool
    image_path: str
    created_at: datetime
    status: str = "pending"
    answer: str | None = None
    approved: bool | None = None
    responder: str | None = None
    resolved_at: datetime | None = None


@dataclass(slots=True)
class _Pending:
    request: BrokerRequest
    future: asyncio.Future[str]
    transport_task: asyncio.Task[None] | None = None


RequestHook = Callable[[BrokerRequest], Awaitable[None]]


class HumanRequestBroker:
    """Authoritative first-response-wins broker for one application run."""

    def __init__(
        self,
        *,
        run_id: str,
        on_requested: RequestHook,
        on_resolved: RequestHook,
        telegram: Any | None = None,
    ) -> None:
        self.run_id = run_id
        self._on_requested = on_requested
        self._on_resolved = on_resolved
        self._telegram = telegram
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        *,
        kind: str,
        question: str,
        context: str,
        url: str,
        company: str,
        role: str,
        options: list[str],
        risk: str,
        image_path: str,
    ) -> str:
        request_id = str(uuid4())
        option_values = tuple(value.strip() for value in options if value.strip())
        request = BrokerRequest(
            request_id=request_id,
            run_id=self.run_id,
            kind=kind,
            question=(
                question.strip().replace(image_path, "the attached image")
                if image_path
                else question.strip()
            ),
            context=context.strip(),
            options=option_values,
            risk=risk,
            allow_free_text=not option_values,
            image_path=image_path,
            created_at=_now(),
        )
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = _Pending(request, future)
        async with self._lock:
            if self._pending:
                raise RuntimeError("Only one human question may be pending for a run.")
            self._pending[request_id] = pending
        await self._on_requested(request)
        if self._telegram is not None:
            pending.transport_task = asyncio.create_task(
                self._wait_for_telegram(
                    request,
                    url=url,
                    company=company,
                    role=role,
                    image_path=image_path,
                ),
                name=f"telegram-hitl-{request_id}",
            )
        try:
            return await future
        except asyncio.CancelledError:
            await self.cancel(request_id)
            raise

    async def resolve_answer(
        self, request_id: str, answer: str, *, responder: str
    ) -> BrokerRequest:
        return await self._resolve(request_id, answer=answer.strip(), responder=responder)

    async def resolve_submission(
        self,
        request_id: str,
        approved: bool,
        *,
        responder: str,
    ) -> BrokerRequest:
        answer = "Approve" if approved else "Reject"
        return await self._resolve(
            request_id,
            answer=answer,
            approved=approved,
            responder=responder,
        )

    async def cancel(self, request_id: str) -> None:
        async with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        if pending.transport_task is not None:
            pending.transport_task.cancel()
        if self._telegram is not None:
            await self._telegram.cancel_request(request_id)
        if not pending.future.done():
            pending.future.cancel()
        await self._on_resolved(replace(pending.request, status="cancelled", resolved_at=_now()))

    async def close(self) -> None:
        for request_id in tuple(self._pending):
            await self.cancel(request_id)

    async def _resolve(
        self,
        request_id: str,
        *,
        answer: str,
        responder: str,
        approved: bool | None = None,
    ) -> BrokerRequest:
        if not answer:
            raise ValueError("human answer must not be empty")
        async with self._lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                raise KeyError(request_id)
            request = pending.request
            if request.options and answer not in request.options:
                raise ValueError("answer must be one of the request options")
            resolved = replace(
                request,
                status="resolved",
                answer=answer,
                approved=approved,
                responder=responder,
                resolved_at=_now(),
            )
        if responder != "telegram" and self._telegram is not None:
            await self._telegram.cancel_request(request_id)
        task = pending.transport_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if not pending.future.done():
            pending.future.set_result(answer)
        await self._on_resolved(resolved)
        return resolved

    async def _wait_for_telegram(
        self,
        request: BrokerRequest,
        *,
        url: str,
        company: str,
        role: str,
        image_path: str,
    ) -> None:
        telegram = self._telegram
        if telegram is None:
            return
        try:
            answer = await telegram.ask_with_id(
                request_id=request.request_id,
                question=request.question,
                context=request.context,
                url=url,
                company=company,
                role=role,
                options=list(request.options),
                risk=request.risk,
                image_path=image_path,
            )
            approved = None
            if request.kind == "submission_approval":
                approved = answer == "Approve"
            await self._resolve(
                request.request_id,
                answer=answer,
                approved=approved,
                responder="telegram",
            )
        except (asyncio.CancelledError, KeyError):
            return


class BrokeredHumanChannel(HumanChannel):
    def __init__(self, broker: HumanRequestBroker, *, url: str) -> None:
        self._broker = broker
        self._url = url

    async def send_artifact(self, *, path: str, caption: str) -> None:
        telegram = self._broker._telegram
        if telegram is not None:
            await telegram.send_artifact_for(
                path=path,
                caption=caption,
                url=self._url,
                company="Z-Apply",
                role="Job application",
            )

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
        return await self._broker.request(
            kind="question",
            question=question,
            context=context,
            url=url,
            company=company,
            role=role,
            options=options or [],
            risk=risk,
            image_path=image_path,
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
        answer = await self._broker.request(
            kind="submission_approval",
            question=question,
            context=context,
            url=url,
            company=company,
            role=role,
            options=["Approve", "Reject"],
            risk="high",
            image_path="",
        )
        return answer == "Approve"
