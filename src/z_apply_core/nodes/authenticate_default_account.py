from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass

from langchain_core.runnables.config import RunnableConfig

from z_apply_core.config import load_settings
from z_apply_core.log_labels import node_info
from z_apply_core.runtime import RunRuntime
from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

logger = logging.getLogger(__name__)

SIMPLIFY_DASHBOARD_URL = "https://simplify.jobs/dashboard"
AUTHENTICATED_MARKERS = (
    "dashboard",
    "applications",
    "profile",
    "settings",
    "logout",
    "sign out",
)
LOGIN_MARKERS = ("log in", "login", "sign in", "email", "password")
BLOCKER_MARKERS = (
    "captcha",
    "recaptcha",
    "verification code",
    "one-time password",
    "otp",
    "two-factor",
    "2fa",
    "multi-factor",
    "mfa",
    "check your email",
    "manual",
)


@dataclass(frozen=True, slots=True)
class AuthClassification:
    status: str
    summary: str


async def authenticate_default_account(
    state: RunState,
    config: RunnableConfig,
) -> dict[str, str]:
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime):
        return {"auth_status": "skipped", "auth_summary": "No live browser runtime is available."}

    sink = _sink_from_config(config)
    settings = load_settings()
    if not settings.has_default_credentials:
        return {
            "auth_status": "skipped",
            "auth_summary": "Default credentials are not configured.",
        }

    original_url = str(state["job_url"])
    await _emit(sink, "started", "Checking Simplify authenticated state.")
    node_info(logger, "authenticate_default_account", "checking Simplify auth state")

    try:
        snapshot = await runtime.browser.tools.call(
            "browser_navigate",
            {"url": SIMPLIFY_DASHBOARD_URL},
        )
        classification = classify_auth_snapshot(snapshot)
        if classification.status == "login_required":
            snapshot = await _try_default_login(
                runtime=runtime,
                username=settings.default_username,
                password=settings.default_password,
                snapshot=snapshot,
            )
            classification = classify_auth_snapshot(snapshot)

        if classification.status == "blocked":
            classification = await _ask_human_to_finish_auth(runtime, snapshot)

        snapshot = await _restore_job_page(runtime, original_url)

        await _emit(sink, classification.status, classification.summary)
        node_info(
            logger,
            "authenticate_default_account",
            "%s: %s",
            classification.status,
            classification.summary,
        )
        return {
            "auth_status": classification.status,
            "auth_summary": classification.summary,
            "snapshot": snapshot,
        }
    except Exception as exc:
        summary = f"Simplify auth check failed: {exc}"
        with contextlib.suppress(Exception):
            await _restore_job_page(runtime, original_url)
        await _emit(sink, "failed", summary)
        node_info(logger, "authenticate_default_account", "%s", summary)
        return {
            "auth_status": "failed",
            "auth_summary": summary,
            "snapshot": str(state.get("snapshot", "")),
        }


def classify_auth_snapshot(snapshot: str) -> AuthClassification:
    text = _normalize(snapshot)
    if _has_any(text, BLOCKER_MARKERS):
        return AuthClassification("blocked", "Simplify requires human auth challenge handling.")
    if _has_any(text, AUTHENTICATED_MARKERS) and not _has_password_field(text):
        return AuthClassification("authenticated", "Simplify appears authenticated.")
    if _has_any(text, LOGIN_MARKERS) and _has_password_field(text):
        return AuthClassification("login_required", "Simplify login form is visible.")
    return AuthClassification("unknown", "Could not determine Simplify authentication state.")


async def _try_default_login(
    *,
    runtime: RunRuntime,
    username: str,
    password: str,
    snapshot: str,
) -> str:
    username_ref = _first_ref_for_labels(snapshot, ("email", "username"))
    password_ref = _first_ref_for_labels(snapshot, ("password",))
    if not username_ref or not password_ref:
        return snapshot

    await runtime.browser.tools.call(
        "browser_type",
        {
            "target": username_ref,
            "element": "default account email field",
            "text": username,
        },
    )
    await runtime.browser.tools.call(
        "browser_type",
        {
            "target": password_ref,
            "element": "default account password field",
            "text": password,
            "submit": True,
        },
    )
    await runtime.browser.tools.call("browser_wait_for", {"time": 2})
    return await runtime.browser.tools.call("browser_snapshot")


async def _restore_job_page(runtime: RunRuntime, original_url: str) -> str:
    restored_snapshot = await runtime.browser.tools.call(
        "browser_navigate",
        {"url": original_url},
    )
    if restored_snapshot.startswith("### Error"):
        return restored_snapshot
    return await runtime.browser.tools.call("browser_snapshot")


async def _ask_human_to_finish_auth(
    runtime: RunRuntime,
    snapshot: str,
) -> AuthClassification:
    channel = runtime.human_channel
    if channel is None:
        return AuthClassification(
            "blocked",
            "Simplify requires human auth challenge handling, but no human channel is configured.",
        )

    await channel.ask(
        question=(
            "Simplify needs manual authentication. Complete the login, CAPTCHA, OTP, "
            "or browser challenge in the live browser, then reply done."
        ),
        context=_clip(snapshot, 2500),
        url=SIMPLIFY_DASHBOARD_URL,
        company="Simplify",
        role="Default account",
        options=["Done"],
        risk="high",
    )
    snapshot_after_human = await runtime.browser.tools.call("browser_snapshot")
    classification = classify_auth_snapshot(snapshot_after_human)
    if classification.status == "authenticated":
        return classification
    return AuthClassification(
        "blocked",
        f"Human auth step returned, but Simplify is still not verified: {classification.summary}",
    )


def _first_ref_for_labels(snapshot: str, labels: tuple[str, ...]) -> str | None:
    for line in snapshot.splitlines():
        lowered = line.lower()
        if "textbox" not in lowered and "input" not in lowered:
            continue
        if not any(label in lowered for label in labels):
            continue
        match = re.search(r"\[ref=([^\]]+)\]", line)
        if match:
            return match.group(1)
    return None


def _has_password_field(text: str) -> bool:
    return "password" in text


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _sink_from_config(config: RunnableConfig) -> FrameworkEventSink | None:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    sink = configurable.get("sink")
    if hasattr(sink, "accept"):
        return sink
    return None


async def _emit(
    sink: FrameworkEventSink | None,
    status: str,
    summary: str,
) -> None:
    if sink is None:
        return
    await sink.accept(
        FrameworkTraceEvent(
            event="auth",
            name="authenticate_default_account",
            data={"status": status, "summary": summary},
            raw={},
        )
    )
