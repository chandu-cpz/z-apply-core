from __future__ import annotations

import contextlib
import importlib
import os
from typing import Any


class VirtualDisplaySession:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._previous_display: str | None = None
        self._display: str | None = None
        self._virtual_display: Any | None = None

    @property
    def display(self) -> str | None:
        return self._display

    def start(self) -> None:
        if not self._enabled:
            self._display = os.environ.get("DISPLAY")
            return

        module = importlib.import_module("camoufox.virtdisplay")
        virtual_display_cls: Any = module.VirtualDisplay
        if hasattr(virtual_display_cls, "xvfb_args"):
            virtual_display_cls.xvfb_args = tuple(
                str(arg).replace("1x1x24", "1920x1080x24") for arg in virtual_display_cls.xvfb_args
            )

        self._previous_display = os.environ.get("DISPLAY")
        self._virtual_display = virtual_display_cls(debug=False)
        self._display = str(self._virtual_display.get())
        os.environ["DISPLAY"] = self._display
        os.environ["MOZ_ENABLE_WAYLAND"] = "0"
        os.environ["GDK_BACKEND"] = "x11"

    def stop(self) -> None:
        if self._virtual_display is not None:
            with contextlib.suppress(Exception):
                self._virtual_display.kill()
        self._virtual_display = None
        self._display = None
        if self._previous_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = self._previous_display
        self._previous_display = None
