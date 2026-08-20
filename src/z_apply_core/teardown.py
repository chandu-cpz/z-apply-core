"""Best-effort teardown helpers.

Teardown steps must never abort a shutdown sequence, but silent
``contextlib.suppress(Exception)`` hid real failures. These wrappers keep the
"never raises" contract while logging every failure with its traceback.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


def best_effort(label: str, fn: Callable[[], Any]) -> None:
    """Run a sync teardown step; log and swallow any exception."""
    try:
        fn()
    except Exception:  # noqa: BLE001 - teardown must not abort, but must be visible
        logger.warning("%s failed", label, exc_info=True)


async def abest_effort(label: str, fn: Callable[[], Awaitable[Any]]) -> None:
    """Run an async teardown step; log and swallow any exception."""
    try:
        await fn()
    except Exception:  # noqa: BLE001 - teardown must not abort, but must be visible
        logger.warning("%s failed", label, exc_info=True)
