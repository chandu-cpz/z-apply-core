from __future__ import annotations

from pathlib import Path
from typing import Any

from z_apply_core.config import load_settings

CORE_ROOT = Path(__file__).resolve().parents[2]


def build_browser_config(run_id: str = "manual") -> dict[str, Any]:
    # Browser authentication and Simplify state belong to Core, not to whichever
    # transport process happened to launch a run (CLI, FastAPI, or tests).
    workspace_dir = CORE_ROOT / ".z-apply"
    profile_dir = workspace_dir / "browser-profile"
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
        "outputDir": str(output_dir),
        "outputMode": "stdout",
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
    return config
