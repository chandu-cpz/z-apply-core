from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler


def configure_logging(console: Console, *, level: int = logging.INFO) -> None:
    # Avoid wiping existing handlers when running under tests where
    # logging is already configured (pytest adds handlers). Only force
    # replacement when no handlers exist yet.
    force = not bool(logging.getLogger().handlers)
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[
            RichHandler(
                console=console,
                rich_tracebacks=True,
                show_path=False,
            )
        ],
        force=force,
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
