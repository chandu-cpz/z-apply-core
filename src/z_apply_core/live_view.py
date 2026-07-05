from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class LiveView:
    port: int | None = None
    x11vnc: subprocess.Popen[bytes] | None = None
    remmina: subprocess.Popen[bytes] | None = None

    def start(self, display: str | None, *, enabled: bool) -> None:
        if not enabled or not display or not display.startswith(":"):
            return
        if shutil.which("x11vnc") is None:
            print("live view skipped: x11vnc is not installed")
            return

        port = _reserve_local_port()
        log_path = Path("/tmp") / f"z-apply-x11vnc-{display.lstrip(':')}.log"
        env = {
            "DISPLAY": display,
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        with log_path.open("wb") as stderr:
            self.x11vnc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    display,
                    "-noshm",
                    "-noxdamage",
                    "-nopw",
                    "-forever",
                    "-listen",
                    "localhost",
                    "-shared",
                    "-rfbport",
                    str(port),
                ],
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=env,
            )

        if not _wait_for_port("127.0.0.1", port, self.x11vnc):
            self.stop()
            print(f"live view skipped: x11vnc did not open port {port}")
            return

        self.port = port
        print(f"live view: vnc://localhost:{port}")
        if shutil.which("remmina") is not None:
            remmina_log = Path("/tmp") / "z-apply-remmina.log"
            with remmina_log.open("wb") as stderr:
                self.remmina = subprocess.Popen(
                    ["remmina", "--enable-fullscreen", "-c", f"vnc://localhost:{port}"],
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    env=os.environ.copy(),
                )

    def stop(self) -> None:
        _terminate(self.remmina)
        _terminate(self.x11vnc)
        self.remmina = None
        self.x11vnc = None
        self.port = None


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_for_port(
    host: str,
    port: int,
    proc: subprocess.Popen[bytes],
    timeout_s: float = 8.0,
) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _terminate(proc: subprocess.Popen[bytes] | None, timeout_s: float = 3.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    with contextlib.suppress(Exception):
        proc.terminate()
        proc.wait(timeout=timeout_s)
    if proc.poll() is None:
        with contextlib.suppress(Exception):
            proc.kill()
