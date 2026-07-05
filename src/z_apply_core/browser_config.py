from __future__ import annotations

from pathlib import Path
from typing import Any


def build_browser_config() -> dict[str, Any]:
    workspace_dir = Path.cwd() / ".z-apply"
    profile_dir = workspace_dir / "browser-profile"
    output_dir = workspace_dir / "browser-artifacts"
    profile_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    return {
        "browser": {
            "provider": "camoufox",
            "browserName": "firefox",
            "userDataDir": str(profile_dir),
            "camoufoxOptions": {"no_viewport": True},
        },
        "outputDir": str(output_dir),
        "outputMode": "stdout",
    }
