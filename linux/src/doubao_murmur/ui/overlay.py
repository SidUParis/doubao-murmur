"""Floating overlay window showing recording status and transcription text.

Mirrors OverlayPanel.swift + OverlayView.swift.
"""

from __future__ import annotations

import logging
import math
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from doubao_murmur.app_state import RecordingState
from doubao_murmur.config import (
    OVERLAY_HEIGHT,
    OVERLAY_MAX_LINES,
    OVERLAY_TEXT_CHARS,
    OVERLAY_WIDTH,
)
from doubao_murmur.ui.windowing import (
    OverlayRole,
    apply_overlay_window_hints,
    present_overlay,
)

logger = logging.getLogger(__name__)

_OVERLAY_CSS = b"""
.overlay-window {
    background: rgba(30, 30, 30, 0.95);
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 0.15);
}
.overlay-label {
    color: white;
    font-size: 14px;
    font-weight: 500;
}
.overlay-label.error {
    color: #ff6666;
}
"""


class Overlay:
    """Floating overlay window showing recording status and transcription text."""

    def __init__(self, app_state) -> None:
        self.app_state = app_state
        self.on_cancel = None  # () -> None
        self._window: Gtk.Window | None = None
        self._label: Gtk.Label | None = None
        self._indicator: Gtk.DrawingArea | None = None

    def _create_window(self) -> None:
        self._window = Gtk.Window()
        self._window.set_title("Open Voice Input Linux")
        self._window.set_decorated(False)
        self._window.set_default_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        self._window.set_resizable(False)
        # GTK3's set_accept_focus() no longer exists in GTK4; focus is
        # negotiated by the window manager.
        self._window.add_css_class("overlay-window")
        apply_overlay_window_hints(self._window, OverlayRole.STATUS)

        # Main container
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        # Recording indicator (pinned to the top so it lines up with the
        # first line of text when the overlay grows to several lines).
        self._indicator = Gtk.DrawingArea()
        self._indicator.set_size_request(14, 14)
        self._indicator.set_valign(Gtk.Align.START)
        self._indicator.set_margin_top(2)
        self._indicator.set_draw_func(self._draw_indicator)
        box.append(self._indicator)

        # Transcription text label
        self._label = Gtk.Label()
        self._label.set_xalign(0)
        self._label.set_yalign(0)
        self._label.set_wrap(True)
        # Chinese has no inter-word spaces, so the default WORD wrap mode
        # finds no break points and the text overflows (then gets cut off
        # with an ellipsis). WORD_CHAR also breaks between characters.
        self._label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        # Fix the wrap column so the (non-resizable) window keeps a stable
        # width and grows in height as the text wraps, instead of being
        # pinned at two lines.
        self._label.set_width_chars(OVERLAY_TEXT_CHARS)
        self._label.set_max_width_chars(OVERLAY_TEXT_CHARS)
        # Grow up to a few lines; only once that cap is reached do the
        # oldest words scroll off (ellipsis at the START), keeping the
        # newest dictated words visible.
        self._label.set_lines(OVERLAY_MAX_LINES)
        self._label.set_ellipsize(Pango.EllipsizeMode.START)
        self._label.add_css_class("overlay-label")
        box.append(self._label)

        self._window.set_child(box)

        # Key press handler for ESC
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self._window.add_controller(key_controller)

        # CSS
        display = Gdk.Display.get_default()
        if display:
            provider = Gtk.CssProvider()
            provider.load_from_data(_OVERLAY_CSS)
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _on_key_pressed(self, controller, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            if self.on_cancel:
                self.on_cancel()
            return True
        return False

    def _draw_indicator(self, area, cr, width, height) -> None:
        """Draw recording indicator: spinner or pulsing red dot."""
        state = self.app_state.recording_state
        if state == RecordingState.STARTING:
            # Spinner
            angle = (time.time() * 4 * math.pi) % (2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.8)
            cr.set_line_width(2)
            cr.arc(width / 2, height / 2, 5, angle, angle + math.pi * 1.6)
            cr.stroke()
            GLib.idle_add(lambda: area.queue_draw() or GLib.SOURCE_REMOVE)
        elif state == RecordingState.RECORDING:
            # Pulsing red dot
            pulse = 0.5 + 0.5 * math.sin(time.time() * 5)
            cr.set_source_rgba(1, 0.2, 0.2, pulse)
            cr.arc(width / 2, height / 2, 5, 0, 2 * math.pi)
            cr.fill()
            GLib.idle_add(lambda: area.queue_draw() or GLib.SOURCE_REMOVE)
        elif state == RecordingState.STOPPING:
            # Static gray dot
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.8)
            cr.arc(width / 2, height / 2, 5, 0, 2 * math.pi)
            cr.fill()

    def show(self) -> None:
        if not self._window:
            self._create_window()
        self._update_content()
        if self._window:
            present_overlay(self._window, OverlayRole.STATUS)

    def hide(self) -> None:
        if self._window:
            self._window.set_visible(False)

    def update_text(self, text: str) -> None:
        """Called when transcription text updates."""
        if not self._label:
            return
        if self.app_state.error_message:
            self._label.set_text(self.app_state.error_message)
            self._label.add_css_class("error")
        elif text:
            self._label.set_text(text)
            self._label.remove_css_class("error")
        else:
            self._label.set_text(self._status_text())
            self._label.remove_css_class("error")

    def _update_content(self) -> None:
        self.update_text(self.app_state.transcription_text)

    def _status_text(self) -> str:
        state = self.app_state.recording_state
        return {
            RecordingState.STARTING: "正在启动语音识别...",
            RecordingState.RECORDING: "正在聆听...",
            RecordingState.STOPPING: "正在进行二遍识别与语义顺滑...",
        }.get(state, "")
