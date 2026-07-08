from __future__ import annotations

from pathlib import Path
from typing import Any

from z_apply_core.config import load_settings


def build_browser_config() -> dict[str, Any]:
    workspace_dir = Path.cwd() / ".z-apply"
    profile_dir = workspace_dir / "browser-profile"
    output_dir = workspace_dir / "browser-artifacts"
    profile_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = load_settings()

    config: dict[str, Any] = {
        "browser": {
            "provider": "camoufox",
            "browserName": "firefox",
            "userDataDir": str(profile_dir),
            "camoufoxOptions": {"no_viewport": True},
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
