"""Constants and configuration for Doubao Murmur Linux port.

Mirrors the fixed parameters from the macOS DoubaoASRClient.swift.
"""

import os
from pathlib import Path

# --- Doubao ASR WebSocket ---

WSS_BASE_URL = "wss://ws-samantha.doubao.com/samantha/audio/asr"

FIXED_QUERY_PARAMS = {
    "version_code": "20800",
    "language": "zh",
    "device_platform": "web",
    "aid": "497858",
    "real_aid": "497858",
    "pkg_type": "release_version",
    "pc_version": "3.12.3",
    "region": "",
    "sys_region": "",
    "samantha_web": "1",
    "use-olympus-account": "1",
    "format": "pcm",
}

ORIGIN = "https://www.doubao.com"
LOGIN_URL = "https://www.doubao.com/chat"

# --- Audio capture ---

AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_DTYPE = "int16"
AUDIO_BLOCKSIZE = 4096  # samples per callback (~256ms at 16kHz)

# --- Auth error detection ---

AUTH_ERROR_CODE = 709599054
AUTH_ERROR_KEYWORDS = [
    "cookie", "auth", "login", "session", "unauthorized", "expired",
]

# --- Paths ---

CONFIG_DIR_NAME = "doubao-murmur"
PARAMS_FILE = "asr_params.json"
KEYBOARD_FILE = "keyboard.json"
PTT_FILE = "ptt_button.json"
PASTE_OVERRIDES_FILE = "paste_overrides.json"


def get_config_dir() -> Path:
    """Get the XDG config directory for the app."""
    config_home = os.environ.get(
        "XDG_CONFIG_HOME", str(Path.home() / ".config")
    )
    app_dir = Path(config_home) / CONFIG_DIR_NAME
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_params_path() -> Path:
    """Get the path to the ASR params JSON file."""
    return get_config_dir() / PARAMS_FILE


def get_keyboard_config_path() -> Path:
    """Get the path to the on-screen keyboard geometry JSON file."""
    return get_config_dir() / KEYBOARD_FILE


def get_ptt_config_path() -> Path:
    """Get the path to the PTT button position JSON file."""
    return get_config_dir() / PTT_FILE


# --- Timeouts ---

# Backstop for waiting out post-stop corrections: the first frame after
# the user stops lands ~2 s later, so 1 s always won and every corrected
# result was discarded.
STOP_SAFETY_TIMEOUT = 3.0  # seconds
DEBOUNCE_INTERVAL = 0.3  # seconds
PASTE_DELAY = 0.05  # seconds between copy and paste simulation
AUTH_EXPIRY_DELAY = 2.0  # seconds before resetting after auth error

# --- Overlay UI ---

OVERLAY_WIDTH = 760
OVERLAY_HEIGHT = 88
# Transcription text wraps within this many "characters" (Pango's average
# char-width unit ~= the old 760px text column) and the overlay grows in
# height up to OVERLAY_MAX_LINES before the oldest words scroll off.
OVERLAY_TEXT_CHARS = 88
OVERLAY_MAX_LINES = 5
PTT_BUTTON_SIZE = 26
PTT_BUTTON_PEEK = 7          # px still visible when tucked against an edge
PTT_BUTTON_SNAP_DIST = 140   # drop within this of a side edge to enable tucking
PTT_BUTTON_IDLE_OPACITY = 0.45

# --- User-Agent for WebView ---

WEBVIEW_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def get_paste_overrides_path() -> Path:
    """Path to the per-WM_CLASS paste-keystroke override file."""
    return get_config_dir() / PASTE_OVERRIDES_FILE


# --- Transcription backend ---

BACKEND_FILE = "backend.json"

# Batch backends upload the whole utterance once the user stops, so a
# 30 s dictation still returns in ~2.8 s (whisper-1, measured) rather
# than scaling with its length. Give the request room beyond that.
BACKEND_REQUEST_TIMEOUT = 120.0


def get_backend_config_path() -> Path:
    """Path to the transcription backend config file."""
    return get_config_dir() / BACKEND_FILE


def load_backend_config() -> dict:
    """Read backend.json, falling back to the built-in doubao backend.

    Shape:
        {"backend": "doubao" | "openai",
         "base_url": "...", "api_key": "...",
         "model": "openai/whisper-1",
         "prompt": "术语表: xdotool, flatpak, ...",
         "language": "zh"}

    `prompt` biases recognition toward names the model would otherwise
    mangle (measured: localStorage/AltGr/Codex all recovered), which is
    the main reason to prefer a batch backend over streaming doubao.
    """
    import json

    path = get_backend_config_path()
    if not path.exists():
        return {"backend": "doubao"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("backend.json must contain an object")
        data.setdefault("backend", "doubao")
        return data
    except Exception as e:  # never let a typo here stop dictation
        import logging

        logging.getLogger(__name__).error(
            "Could not read %s (%s); using the doubao backend", path.name, e
        )
        return {"backend": "doubao"}
