from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DESKTOP_ENV = {
    key: os.environ.get(key, "")
    for key in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_SESSION_TYPE",
        "XDG_CURRENT_DESKTOP",
        "HOME",
        "PATH",
        "XAUTHORITY",
    )
}


@dataclass(slots=True)
class LiveView:
    port: int | None = None
    x11vnc: subprocess.Popen[bytes] | None = None
    remmina: subprocess.Popen[bytes] | None = None
    state_path: Path = Path("/tmp/z-apply-live-view.json")

    def start(self, display: str | None, *, enabled: bool, open_client: bool = True) -> None:
        if not enabled:
            logger.info("Live view disabled")
            return
        self._cleanup_prior_state()
        self.stop()
        if not display or not display.startswith(":"):
            logger.warning("Live view skipped: no virtual X display is available")
            return
        if shutil.which("x11vnc") is None:
            logger.warning("Live view skipped: x11vnc is not installed")
            return

        port = _reserve_local_port()
        log_path = Path("/tmp") / f"z-apply-x11vnc-{display.lstrip(':')}.log"
        logger.info("Starting x11vnc for %s on localhost:%s", display, port)
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
            logger.warning("Live view skipped: x11vnc did not open port %s", port)
            return

        self.port = port
        self._write_state(display)
        logger.info("Live view ready: vnc://localhost:%s", port)
        if not open_client:
            return
        if shutil.which("remmina") is None:
            logger.warning("Remmina is not installed; open vnc://localhost:%s manually", port)
            return

        remmina_log = Path("/tmp") / "z-apply-remmina.log"
        logger.info("Opening Remmina for vnc://localhost:%s", port)
        remmina_env = os.environ.copy()
        for key, value in _DESKTOP_ENV.items():
            if value:
                remmina_env[key] = value
        remmina_env.pop("GDK_BACKEND", None)
        remmina_env.pop("MOZ_ENABLE_WAYLAND", None)

        with remmina_log.open("wb") as stderr:
            self.remmina = subprocess.Popen(
                ["remmina", "--enable-fullscreen", "-c", f"vnc://localhost:{port}"],
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=remmina_env,
            )
        self._write_state(display)
        logger.info("Remmina launched with pid %s; log: %s", self.remmina.pid, remmina_log)

    def stop(self) -> None:
        _terminate(self.remmina)
        _terminate(self.x11vnc)
        self.remmina = None
        self.x11vnc = None
        self.port = None
        with contextlib.suppress(Exception):
            self.state_path.unlink(missing_ok=True)

    def _write_state(self, display: str) -> None:
        data = {
            "display": display,
            "port": self.port,
            "x11vnc_pid": self.x11vnc.pid if self.x11vnc else None,
            "remmina_pid": self.remmina.pid if self.remmina else None,
        }
        with contextlib.suppress(Exception):
            self.state_path.write_text(json.dumps(data), encoding="utf-8")

    def _cleanup_prior_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        _terminate_pid(_int_or_zero(data.get("remmina_pid")), "remmina", "remmina")
        _terminate_pid(_int_or_zero(data.get("x11vnc_pid")), "x11vnc", "x11vnc")
        with contextlib.suppress(Exception):
            self.state_path.unlink(missing_ok=True)


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


def _terminate_pid(pid: int, token: str, name: str, timeout_s: float = 3.0) -> None:
    if pid <= 0 or not _pid_matches(pid, token):
        return
    logger.info("Stopping stale %s process pid=%s", name, pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception as exc:
        logger.warning("Failed to terminate stale %s pid=%s: %s", name, pid, exc)
        return

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_matches(pid, token):
            return
        time.sleep(0.1)

    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def _pid_matches(pid: int, token: str) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return token in cmdline


def _int_or_zero(value: object) -> int:
    try:
        return int(str(value or 0))
    except ValueError:
        return 0
