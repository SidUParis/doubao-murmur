"""Non-sensitive local settings used by the controller-only UI."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_NAME = "doubao-murmur"
PTT_FILE = "ptt_button.json"

DEBOUNCE_INTERVAL = 0.3
PTT_BUTTON_SIZE = 26
PTT_BUTTON_PEEK = 7
PTT_BUTTON_SNAP_DIST = 140
PTT_BUTTON_IDLE_OPACITY = 0.45


def get_ptt_config_path() -> Path:
    """Return the Flatpak-private floating-button position file."""
    config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    app_dir = Path(config_home) / CONFIG_DIR_NAME
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir / PTT_FILE
