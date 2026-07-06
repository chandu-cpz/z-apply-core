from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler


def configure_logging(console: Console, *, level: int = logging.INFO) -> None:
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
        force=True,
    )
