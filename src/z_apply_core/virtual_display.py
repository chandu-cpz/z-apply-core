from __future__ import annotations

import importlib
import logging
from typing import Any

from z_apply_core.teardown import best_effort

logger = logging.getLogger(__name__)


class VirtualDisplaySession:
    """Per-run Xvfb display without process-global side effects.

    The display string is exposed via ``display`` and passed explicitly to
    ``build_browser_config(display=...)`` which injects it via
    ``launchOptions.env``. No ``os.environ`` mutation occurs so concurrent
    runs never race on the global ``DISPLAY``.
    """

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._display: str | None = None
        self._virtual_display: Any | None = None

    @property
    def display(self) -> str | None:
        return self._display

    def start(self) -> None:
        if not self._enabled:
            self._display = None
            return

        module = importlib.import_module("camoufox.virtdisplay")
        virtual_display_cls: Any = module.VirtualDisplay
        if hasattr(virtual_display_cls, "xvfb_args"):
            virtual_display_cls.xvfb_args = tuple(
                str(arg).replace("1x1x24", "1920x1080x24") for arg in virtual_display_cls.xvfb_args
            )

        self._virtual_display = virtual_display_cls(debug=False)
        self._display = str(self._virtual_display.get())

    def stop(self) -> None:
        if self._virtual_display is not None:
            best_effort("virtual display kill", self._virtual_display.kill)
        self._virtual_display = None
        self._display = None
