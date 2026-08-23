"""Constants and configuration for the Open Voice Input Linux transition app.

Mirrors the fixed parameters from the macOS DoubaoASRClient.swift.
"""

import json
import os
import stat
import tempfile
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
VOLCENGINE_FILE = "volcengine.json"
PERSONAL_VOCABULARY_FILE = "personal_vocabulary.json"

# Keep the request-level context deliberately small even though the provider
# accepts much larger managed tables.  This file is loaded and sent with every
# utterance, so a hard local budget prevents accidental oversized requests.
MAX_PERSONAL_VOCABULARY_TERMS = 200
MAX_PERSONAL_VOCABULARY_TERM_LENGTH = 64
MAX_VOLCENGINE_API_KEY_LENGTH = 4096

# Keep credentials out of backend.json: that file is useful to share when
# comparing recognition settings, whereas this file is deliberately local.
_VOLCENGINE_SECRET_KEYS = frozenset(
    {"api_key", "app_id", "app_key", "access_token", "access_key"}
)

# The settings UI intentionally exposes only the credential.  These defaults
# are the known-good input-method profile: useful live hypotheses followed by
# the higher-accuracy non-streaming correction when the user stops speaking.
VOLCENGINE_RECOMMENDED_BACKEND = {
    "backend": "volcengine",
    "endpoint": "bigmodel_async",
    "resource_id": "volc.seedasr.sauc.duration",
    "language": "zh-CN",
    "enable_nonstream": True,
    "enable_ddc": True,
    "enable_itn": True,
    "enable_punc": True,
    "show_utterances": True,
    "result_type": "full",
    "chunk_ms": 200,
    "final_result_timeout": 20.0,
    "max_pending_audio_seconds": 10,
    "max_recording_seconds": 600,
}

# Batch backends upload the whole utterance once the user stops, so a
# 30 s dictation still returns in ~2.8 s (whisper-1, measured) rather
# than scaling with its length. Give the request room beyond that.
BACKEND_REQUEST_TIMEOUT = 120.0


def get_backend_config_path() -> Path:
    """Path to the transcription backend config file."""
    return get_config_dir() / BACKEND_FILE


def get_volcengine_config_path() -> Path:
    """Path to the local Volcengine credential/config file."""
    return get_config_dir() / VOLCENGINE_FILE


def get_personal_vocabulary_path() -> Path:
    """Path to the explicit, never automatically learned personal terms."""
    return get_config_dir() / PERSONAL_VOCABULARY_FILE


def _atomic_write_json(path: Path, data: dict, mode: int = 0o600) -> None:
    """Durably replace one JSON file without exposing partial contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only the app-specific directory is made private.  Never chmod the user's
    # XDG config root (usually ~/.config), which is shared by other programs.
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    open_fd: int | None = fd
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            open_fd = None
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        # os.replace keeps the temporary file mode.  Enforce it once more in
        # case a platform or mocked filesystem behaves differently.
        os.chmod(path, mode)
    except Exception:
        if open_fd is not None:
            os.close(open_fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _harden_private_json_path(path: Path) -> bool:
    """Make an existing user-owned regular file private before reading it.

    Older releases created credential and learned-vocabulary files with the
    process umask, which can leave them world-readable.  Refuse symlinks and
    foreign-owned paths, then tighten only this application's directory and
    the target file.  The user's XDG config root is never chmodded.
    """
    try:
        parent = path.parent
        parent_stat = parent.lstat()
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent.is_symlink()
            or parent_stat.st_uid != os.getuid()
        ):
            return False
        os.chmod(parent, 0o700)

        file_stat = path.lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or path.is_symlink()
            or file_stat.st_uid != os.getuid()
        ):
            return False
        os.chmod(path, 0o600)
        return True
    except (FileNotFoundError, OSError):
        return False


def save_volcengine_api_key(api_key: str) -> None:
    """Store one API key and select the recommended Volcengine profile.

    The credential is replaced first.  If the process stops between the two
    atomic writes, the previous backend remains active instead of selecting a
    backend whose credential has not yet been persisted.
    """
    if not isinstance(api_key, str):
        raise ValueError("Volcengine API Key must be text")
    normalized = api_key.strip()
    if not normalized:
        raise ValueError("Volcengine API Key is empty")
    if len(normalized) > MAX_VOLCENGINE_API_KEY_LENGTH:
        raise ValueError("Volcengine API Key is too long")
    if any(character in normalized for character in "\r\n\x00"):
        raise ValueError("Volcengine API Key contains invalid characters")

    _atomic_write_json(
        get_volcengine_config_path(), {"api_key": normalized}, mode=0o600
    )
    _atomic_write_json(
        get_backend_config_path(),
        dict(VOLCENGINE_RECOMMENDED_BACKEND),
        mode=0o600,
    )


def has_volcengine_api_key() -> bool:
    """Return whether a non-empty API key is stored, without exposing it."""
    settings = load_volcengine_config({})
    return bool(str(settings.get("api_key") or "").strip())


def _normalize_personal_vocabulary(
    terms: object, *, strict: bool
) -> list[str]:
    if isinstance(terms, str):
        candidates = terms.splitlines()
    elif isinstance(terms, (list, tuple)):
        candidates = terms
    else:
        raise ValueError("personal vocabulary must be text or a list")

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            if strict:
                raise ValueError("personal vocabulary terms must be text")
            continue
        term = candidate.strip()
        if not term:
            continue
        if any(character in term for character in "\r\n\x00"):
            if strict:
                raise ValueError("personal vocabulary term is not one line")
            continue
        if len(term) > MAX_PERSONAL_VOCABULARY_TERM_LENGTH:
            if strict:
                raise ValueError("personal vocabulary term is too long")
            continue

        identity = term.casefold()
        if identity in seen:
            continue
        if len(normalized) >= MAX_PERSONAL_VOCABULARY_TERMS:
            if strict:
                raise ValueError("personal vocabulary has too many terms")
            break
        seen.add(identity)
        normalized.append(term)
    return normalized


def load_personal_vocabulary() -> list[str]:
    """Load the explicit vocabulary, sanitizing malformed individual terms."""
    import logging

    path = get_personal_vocabulary_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "terms" not in data:
            raise ValueError("personal vocabulary must contain terms")
        return _normalize_personal_vocabulary(data["terms"], strict=False)
    except Exception as error:
        # Never interpolate the exception: a malformed JSON decoder message or
        # filesystem wrapper could contain user-supplied vocabulary text.
        logging.getLogger(__name__).warning(
            "Could not read %s (%s); using an empty personal vocabulary",
            path.name,
            error.__class__.__name__,
        )
        return []


def save_personal_vocabulary(terms: object) -> list[str]:
    """Normalize and atomically store explicit terms with mode 0600."""
    normalized = _normalize_personal_vocabulary(terms, strict=True)
    _atomic_write_json(
        get_personal_vocabulary_path(),
        {"terms": normalized},
        mode=0o600,
    )
    return normalized


def load_backend_config() -> dict:
    """Read backend.json, falling back to the built-in doubao backend.

    Shape:
        {"backend": "doubao" | "openai" | "volcengine",
         "base_url": "...", "api_key": "...",
         "model": "openai/whisper-1",
         "prompt": "术语表: xdotool, flatpak, ...",
         "language": "zh"}

    Volcengine credentials are deliberately loaded separately from
    volcengine.json; backend.json may only override its non-secret settings.

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


def load_volcengine_config(backend_settings: dict | None = None) -> dict:
    """Load Volcengine credentials and merge shareable backend settings.

    Credentials live in ``volcengine.json`` so ``backend.json`` can remain a
    non-secret backend selector/tuning file.  Safe values from backend.json
    win, allowing endpoint, language and latency tuning without duplicating
    the API key.  Credential-looking keys in backend.json are intentionally
    ignored.
    """
    import json
    import logging

    path = get_volcengine_config_path()
    credentials: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("volcengine.json must contain an object")
            credentials = data
        except Exception as e:
            logging.getLogger(__name__).error(
                "Could not read %s (%s); Volcengine credentials unavailable",
                path.name,
                e,
            )

    settings = (
        backend_settings
        if isinstance(backend_settings, dict)
        else load_backend_config()
    )
    merged = dict(credentials)
    for key, value in settings.items():
        if key != "backend" and key not in _VOLCENGINE_SECRET_KEYS:
            merged[key] = value
    return merged


GLOSSARY_FILE = "glossary.json"


def get_glossary_path() -> Path:
    """Path to the learned transcription vocabulary."""
    return get_config_dir() / GLOSSARY_FILE
