"""Copy text to clipboard and simulate Ctrl+V paste.

Mirrors PasteHelper.swift.

Methods (in priority order):
1. wl-copy (Wayland clipboard) + ydotool (paste simulation)
2. xclip/xsel (X11 clipboard) + xdotool (X11 paste simulation)
3. GTK clipboard API as last resort
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import subprocess
import time

from doubao_murmur.config import PASTE_DELAY, get_paste_overrides_path
from doubao_murmur.host_tools import command_candidates

logger = logging.getLogger(__name__)

# Terminal emulators interpret Ctrl+V as a control sequence; their paste
# shortcut is Ctrl+Shift+V instead. Matched against the focused window's
# WM class (lowercased).
_TERMINAL_WM_CLASSES = {
    "konsole",
    "yakuake",
    "alacritty",
    "kitty",
    "foot",
    "wezterm",
    "org.wezfurlong.wezterm",
    "gnome-terminal-server",
    "xterm",
    "urxvt",
    "st",
    "terminator",
    "tilix",
    "xfce4-terminal",
    "lxterminal",
    "deepin-terminal",
    "qterminal",
    "io.elementary.terminal",
    "ghostty",
    "com.mitchellh.ghostty",
    "warp",
    "warp-terminal",
    "dev.warp.warp",
}


@dataclass(frozen=True)
class PasteTarget:
    """Window-level focus identity captured when dictation starts.

    ``None`` means the platform cannot verify the active window. Passing such
    a target deliberately degrades to clipboard-only instead of guessing.
    """

    window_id: str | None


class PasteHelper:
    """Copy text to clipboard and simulate paste keystroke."""

    @staticmethod
    def copy_and_paste(
        text: str, *, target: PasteTarget | None = None
    ) -> bool:
        if not text:
            return False
        if target is not None and not PasteHelper._target_matches(target):
            PasteHelper._copy_to_clipboard(text)
            logger.warning(
                "Paste target unavailable or changed; copied without pasting"
            )
            return False
        if not PasteHelper._copy_to_clipboard(text):
            # Never inject Ctrl+V after a failed copy: doing so would paste
            # whatever unrelated value was already in the user's clipboard.
            logger.error("Clipboard update failed; skipped key injection")
            return False
        time.sleep(PASTE_DELAY)
        # Re-check after the clipboard write and delay to close the most common
        # focus-change race immediately before synthetic key injection.
        if target is not None and not PasteHelper._target_matches(target):
            logger.warning(
                "Paste target changed after copy; skipped key injection"
            )
            return False
        return PasteHelper._simulate_paste()

    @staticmethod
    def capture_target() -> PasteTarget:
        """Capture the current X11 target, or an unverifiable safe target."""
        return PasteTarget(PasteHelper._focused_window_id())

    @staticmethod
    def _target_matches(target: PasteTarget) -> bool:
        return (
            target.window_id is not None
            and PasteHelper._focused_window_id() == target.window_id
        )

    @staticmethod
    def _focused_window_id() -> str | None:
        """Return a normalized X11 active-window ID when it is available."""
        for command in command_candidates("xdotool"):
            try:
                result = subprocess.run(
                    command + ["getactivewindow"],
                    capture_output=True,
                    check=True,
                    timeout=3,
                )
                window_id = int(result.stdout.decode().strip(), 0)
                if window_id > 0:
                    return str(window_id)
            except Exception as e:
                logger.debug("Active window ID detection failed: %s", e)
        return None

    @staticmethod
    def copy_only(text: str) -> bool:
        if not text:
            return False
        return PasteHelper._copy_to_clipboard(text)

    @staticmethod
    def _copy_to_clipboard(text: str) -> bool:
        """Copy text to system clipboard."""
        # Try Wayland first
        for command in command_candidates("wl-copy"):
            try:
                subprocess.run(
                    command,
                    input=text.encode(),
                    check=True,
                    timeout=3,
                )
                logger.info("Copied to clipboard via wl-copy")
                return True
            except Exception as e:
                logger.warning("wl-copy failed: %s", e)

        # Try X11
        for command in command_candidates("xclip"):
            try:
                subprocess.run(
                    command + ["-selection", "clipboard"],
                    input=text.encode(),
                    check=True,
                    timeout=3,
                )
                logger.info("Copied to clipboard via xclip")
                return True
            except Exception as e:
                logger.warning("xclip failed: %s", e)

        # Try xsel
        for command in command_candidates("xsel"):
            try:
                subprocess.run(
                    command + ["--clipboard", "--input"],
                    input=text.encode(),
                    check=True,
                    timeout=3,
                )
                logger.info("Copied to clipboard via xsel")
                return True
            except Exception as e:
                logger.warning("xsel failed: %s", e)

        # GTK clipboard as last resort
        try:
            from gi.repository import Gdk

            display = Gdk.Display.get_default()
            if display:
                clipboard = display.get_clipboard()
                clipboard.set(text)
                logger.info("Copied to clipboard via GTK")
                return True
        except Exception as e:
            logger.error("All clipboard methods failed: %s", e)
        return False

    @staticmethod
    def _simulate_paste() -> bool:
        """Simulate the paste keystroke for the focused window.

        Terminals use Ctrl+Shift+V; everything else uses Ctrl+V.
        """
        use_shift = PasteHelper._paste_needs_shift()

        # Try ydotool (works on both Wayland and X11)
        # Keycodes: 29=LEFTCTRL, 42=LEFTSHIFT, 47=V
        if use_shift:
            ydotool_keys = ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]
        else:
            ydotool_keys = ["29:1", "47:1", "47:0", "29:0"]
        for command in command_candidates("ydotool"):
            try:
                subprocess.run(
                    command + ["key"] + ydotool_keys,
                    check=True,
                    timeout=3,
                )
                logger.info("Paste simulated via ydotool")
                return True
            except Exception as e:
                logger.warning("ydotool failed: %s", e)

        # Try wtype (Wayland virtual keyboard)
        if use_shift:
            wtype_args = ["-M", "ctrl", "-M", "shift", "-P", "v",
                          "-m", "shift", "-m", "ctrl"]
        else:
            wtype_args = ["-M", "ctrl", "-P", "v", "-m", "ctrl"]
        for command in command_candidates("wtype"):
            try:
                subprocess.run(
                    command + wtype_args,
                    check=True,
                    timeout=3,
                )
                logger.info("Paste simulated via wtype")
                return True
            except Exception as e:
                logger.warning("wtype failed: %s", e)

        # Try xdotool (X11 only)
        xdotool_key = "ctrl+shift+v" if use_shift else "ctrl+v"
        for command in command_candidates("xdotool"):
            try:
                subprocess.run(
                    command + ["key", xdotool_key],
                    check=True,
                    timeout=3,
                )
                logger.info("Paste simulated via xdotool (%s)", xdotool_key)
                return True
            except Exception as e:
                logger.warning("xdotool failed: %s", e)

        logger.error("No paste simulation method available")
        logger.info(
            "Text was copied to clipboard but could not auto-paste. "
            "Install ydotool or wtype for auto-paste."
        )
        return False

    @staticmethod
    def _paste_needs_shift() -> bool:
        """Whether the focused window should get Ctrl+Shift+V.

        A per-WM_CLASS override file comes first, because the WM_CLASS of
        the local window does not always say what will receive the keys.
        A remote-desktop client forwards them verbatim, so what matters is
        the application focused on the far side, which is invisible from
        here: Ctrl+V reaches the remote TUI directly, where Claude Code
        reads the clipboard but Codex only accepts images and reports
        "failed to paste image". Ctrl+Shift+V is instead consumed by the
        remote terminal, which pastes text that any TUI accepts.

        Overrides live in paste_overrides.json as {WM_CLASS: combo}, with
        combo "ctrl+v" or "ctrl+shift+v"; WM_CLASS is matched lowercased
        against every entry the focused window reports.
        """
        wm_classes = PasteHelper._focused_window_classes()
        overrides = PasteHelper._load_paste_overrides()
        for wm_class in wm_classes:
            combo = overrides.get(wm_class)
            if combo:
                logger.info(
                    "Paste override for %s: %s", wm_class, combo
                )
                return "shift" in combo.lower()

        if not wm_classes:
            return False
        is_terminal = any(c in _TERMINAL_WM_CLASSES for c in wm_classes)
        logger.info(
            "Focused window class: %s (terminal=%s)",
            "/".join(wm_classes),
            is_terminal,
        )
        return is_terminal

    @staticmethod
    def _load_paste_overrides() -> dict[str, str]:
        path = get_paste_overrides_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                str(k).strip().lower(): str(v)
                for k, v in data.items()
                if isinstance(data, dict)
            }
        except Exception as e:
            logger.warning("Could not read %s: %s", path.name, e)
            return {}

    @staticmethod
    def _focused_window_classes() -> list[str]:
        """Lowercased WM_CLASS entries of the focused window (X11).

        `getwindowclassname` only exists in recent xdotool releases;
        Debian/Ubuntu still ship 3.20160805, where it exits with
        "Unknown command". Detection then always failed, so every paste
        used Ctrl+V -- which terminals swallow instead of pasting. Fall
        back to xprop, which lives in x11-utils and is present on any
        desktop that has xdotool.
        """
        for command in command_candidates("xdotool"):
            try:
                result = subprocess.run(
                    command + ["getactivewindow", "getwindowclassname"],
                    capture_output=True,
                    check=True,
                    timeout=3,
                )
                wm_class = result.stdout.decode().strip().lower()
                if wm_class:
                    return [wm_class]
            except Exception as e:
                logger.debug("xdotool getwindowclassname failed: %s", e)

        window_id = ""
        for command in command_candidates("xdotool"):
            try:
                result = subprocess.run(
                    command + ["getactivewindow"],
                    capture_output=True,
                    check=True,
                    timeout=3,
                )
                window_id = result.stdout.decode().strip()
                break
            except Exception as e:
                logger.warning("Active window detection failed: %s", e)
        if not window_id:
            return []

        for command in command_candidates("xprop"):
            try:
                result = subprocess.run(
                    command + ["-id", window_id, "WM_CLASS"],
                    capture_output=True,
                    check=True,
                    timeout=3,
                )
                # WM_CLASS(STRING) = "terminator", "Terminator"
                values = result.stdout.decode().partition("=")[2]
                return [
                    part.strip().strip('"').lower()
                    for part in values.split(",")
                    if part.strip()
                ]
            except Exception as e:
                logger.warning("xprop WM_CLASS lookup failed: %s", e)
        return []
