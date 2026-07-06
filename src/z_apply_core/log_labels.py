from __future__ import annotations

import logging
from typing import Any


def node_info(logger: logging.Logger, node: str, message: str, *args: Any) -> None:
    logger.info(
        "[black on cyan] NODE [/][cyan] %s[/] " + message,
        node,
        *args,
        extra={"markup": True},
    )


def run_info(logger: logging.Logger, message: str, *args: Any) -> None:
    logger.info("[black on green] RUN [/] " + message, *args, extra={"markup": True})
