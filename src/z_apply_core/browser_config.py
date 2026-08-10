from __future__ import annotations

from pathlib import Path
from typing import Any

from z_apply_core.config import CORE_ROOT, load_settings


def build_browser_config(
    run_id: str = "manual", *, profile_dir: Path | None = None
) -> dict[str, Any]:
    # Browser authentication and Simplify state belong to Core, not to whichever
    # transport process happened to launch a run (CLI, FastAPI, or tests).
    #
    # ``profile_dir`` selects the Firefox profile the browser launches on.
    # Multi-browser runs pass a per-run slot dir (profile_pool.ProfileSlotPool);
    # when omitted, the legacy shared master profile is used.
    workspace_dir = CORE_ROOT / ".z-apply"
    profile_dir = profile_dir or workspace_dir / "browser-profile"
    output_dir = workspace_dir / "runs" / run_id / "browser-artifacts"
    profile_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    addon_path = Path(settings.simplify_addon_path).expanduser().resolve()
    if not addon_path.is_dir():
        raise ValueError(f"Configured Simplify addon directory does not exist: {addon_path}")

    config: dict[str, Any] = {
        "browser": {
            "provider": "camoufox",
            "browserName": "firefox",
            "userDataDir": str(profile_dir),
            "camoufoxOptions": {
                "browser": settings.camoufox_browser,
                "no_viewport": True,
                "addons": [str(addon_path)],
            },
        },
        "timeouts": {"navigation": 120_000},
        "outputDir": str(output_dir),
        "outputMode": "stdout",
        "imageResponses": "omit",
        # Mutation tool responses must NOT re-ship the full ARIA tree: the
        # runtime builds its own bounded post-action receipt from a fresh
        # snapshot, so the MCP-side duplicate would be pure token waste.
        # browser_snapshot itself is unaffected (it uses mode "explicit").
        "snapshot": {"mode": "none"},
        "console": {"level": "error"},
    }
    secrets = {
        name: value
        for name, value in {
            "DEFAULT_USERNAME": settings.default_username,
            "DEFAULT_PASSWORD": settings.default_password,
        }.items()
        if value
    }
    if secrets:
        config["secrets"] = secrets
    # Non-lookupable redaction set: the browser layer masks these values in
    # LLM-visible text but they are NOT exposed through the credential lookup.
    # The owner email is known at build time; runtime candidate PII is out of
    # scope here and handled at the human-ask boundary instead.
    redact_values = {
        name: value
        for name, value in {
            "DEFAULT_USERNAME": settings.default_username,
        }.items()
        if value
    }
    if redact_values:
        config["redact_values"] = redact_values
    return config
