"""Always-on-top push-to-talk button (GTK4).

This is the PRIMARY input method on Wayland because global hotkeys are
restricted by the compositor security model.

Design:
- Small circular button, docked wherever the user last dragged it
- Semi-transparent while idle, opaque on hover and while recording
- Dropped near a side edge it tucks away, leaving a few pixels visible,
  and slides back out when the pointer reaches it
- Shows whenever the app is logged in
- Click to toggle recording, drag to move
"""

from __future__ import annotations

import json
import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk

from doubao_murmur.config import (
    PTT_BUTTON_IDLE_OPACITY,
    PTT_BUTTON_PEEK,
    PTT_BUTTON_SIZE,
    PTT_BUTTON_SNAP_DIST,
    get_ptt_config_path,
)
from doubao_murmur.ui.windowing import (
    OverlayRole,
    apply_overlay_window_hints,
    present_overlay,
    x11_move,
    x11_pointer,
)

logger = logging.getLogger(__name__)

_DRAG_THRESHOLD = 4  # px of travel before a press counts as a drag

_PTT_CSS = ("""
.ptt-window {
    background: transparent;
}
.ptt-button {
    background: rgba(40, 40, 40, 0.85);
    border-radius: %(radius)dpx;
    border: 1px solid rgba(255, 255, 255, 0.3);
    color: white;
    font-size: %(font)dpx;
    min-width: %(size)dpx;
    min-height: %(size)dpx;
    padding: 0;
}
.ptt-button:hover {
    background: rgba(60, 60, 60, 0.9);
}
.ptt-button.recording {
    background: rgba(200, 40, 40, 0.85);
    border-color: rgba(255, 100, 100, 0.6);
}
""" % {
    "size": PTT_BUTTON_SIZE,
    "radius": PTT_BUTTON_SIZE // 2 + 2,
    "font": max(10, PTT_BUTTON_SIZE // 2),
}).encode()


class OverlayButton:
    """Small always-on-top push-to-talk button."""

    def __init__(self, on_press, on_cancel) -> None:
        self.on_press = on_press
        self.on_cancel = on_cancel
        self._window: Gtk.Window | None = None
        self._button: Gtk.Button | None = None

        # Python-side source of truth for the position, like the on-screen
        # keyboard: GTK4 has no window placement API, so geometry is applied
        # through Xlib and tracked here.
        self._x: int | None = None
        self._y: int | None = None
        self._edge: str | None = None  # "left" | "right" | None
        self._recording = False
        self._dragging = False
        self._anchor_pointer: tuple[int, int] | None = None
        self._anchor_geom: tuple[int, int] | None = None

    # -- construction -------------------------------------------------------

    def create(self) -> None:
        """Create the GTK window and button."""
        self._window = Gtk.Window()
        self._window.set_title("Doubao Murmur PTT")
        self._window.set_decorated(False)
        self._window.set_default_size(PTT_BUTTON_SIZE + 4, PTT_BUTTON_SIZE + 4)
        self._window.set_resizable(False)
        self._window.add_css_class("ptt-window")
        apply_overlay_window_hints(self._window, OverlayRole.PTT)

        # Create circular button
        self._button = Gtk.Button()
        self._button.add_css_class("ptt-button")
        self._button.set_label("\U0001f3a4")  # 🎤
        self._button.connect("clicked", self._on_clicked)
        self._window.set_child(self._button)

        # Key press handler for ESC
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self._window.add_controller(key_controller)

        # Drag to move. CAPTURE so the gesture sees the press before
        # Gtk.Button does, but the sequence is only claimed once the
        # pointer has actually travelled, so a plain click still toggles.
        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._on_move_begin)
        drag.connect("drag-update", self._on_move_update)
        drag.connect("drag-end", self._on_move_end)
        self._window.add_controller(drag)

        # Hover reveals a tucked button.
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_pointer_enter)
        motion.connect("leave", self._on_pointer_leave)
        self._window.add_controller(motion)

        self._load_position()

        # Apply CSS
        display = Gdk.Display.get_default()
        if display:
            provider = Gtk.CssProvider()
            provider.load_from_data(_PTT_CSS)
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    # -- position persistence ----------------------------------------------

    def _load_position(self) -> None:
        path = get_ptt_config_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._x = int(data["x"])
            self._y = int(data["y"])
            edge = data.get("edge")
            self._edge = edge if edge in ("left", "right") else None
        except Exception as e:
            logger.warning("Could not load PTT position: %s", e)

    def _save_position(self) -> None:
        if self._x is None or self._y is None:
            return
        try:
            get_ptt_config_path().write_text(
                json.dumps({"x": self._x, "y": self._y, "edge": self._edge}),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Could not save PTT position: %s", e)

    def _default_position(self) -> None:
        """Bottom-centre of the monitor the button was mapped onto."""
        if not self._window:
            return
        display = self._window.get_display()
        surface = self._window.get_surface()
        monitor = None
        if display is not None:
            if surface is not None and hasattr(
                display, "get_monitor_at_surface"
            ):
                monitor = display.get_monitor_at_surface(surface)
            if monitor is None:
                monitor = display.get_monitors().get_item(0)
        if monitor is None:
            return
        geo = monitor.get_geometry()
        size = PTT_BUTTON_SIZE + 4
        self._x = geo.x + (geo.width - size) // 2
        self._y = geo.y + geo.height - size - 24

    # -- edge tucking -------------------------------------------------------

    def _monitor_geometry(self):
        """Geometry of the monitor holding the button, or None if it is
        not on any of them.

        Returning None rather than silently falling back matters: a saved
        position becomes stale whenever the layout changes (a display
        unplugged, or the same screens re-arranged after a reboot), and
        computing an edge against the wrong monitor puts the button
        somewhere the user never left it.
        """
        if not self._window or self._x is None or self._y is None:
            return None
        display = self._window.get_display()
        if display is None:
            return None
        monitors = display.get_monitors()
        for i in range(monitors.get_n_items()):
            geo = monitors.get_item(i).get_geometry()
            if (
                geo.x <= self._x < geo.x + geo.width
                and geo.y <= self._y < geo.y + geo.height
            ):
                return geo
        return None

    def _update_edge(self) -> None:
        """Flush the position to the nearest side edge when close to one."""
        geo = self._monitor_geometry()
        if geo is None or self._x is None:
            self._edge = None
            return
        size = PTT_BUTTON_SIZE + 4
        if self._x - geo.x <= PTT_BUTTON_SNAP_DIST:
            self._edge = "left"
            self._x = geo.x
        elif (geo.x + geo.width) - (self._x + size) <= PTT_BUTTON_SNAP_DIST:
            self._edge = "right"
            self._x = geo.x + geo.width - size
        else:
            self._edge = None

    def _apply_position(self, tucked: bool) -> None:
        """Place the window, either at its home spot or tucked away."""
        if not self._window or self._x is None or self._y is None:
            return
        x = self._x
        if tucked and self._edge:
            geo = self._monitor_geometry()
            size = PTT_BUTTON_SIZE + 4
            if geo is not None:
                if self._edge == "left":
                    x = geo.x - (size - PTT_BUTTON_PEEK)
                else:
                    x = geo.x + geo.width - PTT_BUTTON_PEEK
        x11_move(self._window, x, self._y)
        self._window.set_opacity(
            PTT_BUTTON_IDLE_OPACITY if tucked and self._edge else 1.0
        )

    def _busy(self) -> bool:
        """A drag moves the window under the cursor, which fires
        enter/leave; acting on those would fight the drag."""
        return self._dragging or self._recording

    def _on_pointer_enter(self, *_args) -> None:
        if not self._busy():
            self._apply_position(tucked=False)

    def _on_pointer_leave(self, *_args) -> None:
        if not self._busy():
            self._apply_position(tucked=True)

    # -- move ---------------------------------------------------------------

    def _on_move_begin(self, _gesture, _x, _y) -> None:
        self._dragging = False
        self._anchor_pointer = None
        self._anchor_geom = None

    def _on_move_update(self, gesture, ox, oy) -> None:
        if not self._dragging:
            if abs(ox) < _DRAG_THRESHOLD and abs(oy) < _DRAG_THRESHOLD:
                return
            if self._x is None or self._y is None:
                return
            self._dragging = True
            # Cancel the pending button press so no toggle fires.
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._anchor_pointer = x11_pointer()
            self._anchor_geom = (self._x, self._y)
            if self._window:
                self._window.set_opacity(1.0)

        if not self._anchor_pointer or not self._anchor_geom:
            return
        # Driven by the absolute pointer, not the gesture offsets: those
        # are widget-relative, so moving the window shifts the pointer
        # within it and the next offset shrinks -- a feedback loop that
        # makes the button oscillate and trail the cursor.
        cur = x11_pointer()
        if cur is None:
            return
        self._x = self._anchor_geom[0] + (cur[0] - self._anchor_pointer[0])
        self._y = self._anchor_geom[1] + (cur[1] - self._anchor_pointer[1])
        if self._window:
            x11_move(self._window, self._x, self._y)

    def _on_move_end(self, _gesture, _ox, _oy) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._anchor_pointer = None
        self._anchor_geom = None
        self._update_edge()
        self._save_position()
        logger.info(
            "PTT button moved to (%s, %s), edge=%s",
            self._x, self._y, self._edge,
        )
        self._apply_position(tucked=True)

    # -- interaction --------------------------------------------------------

    def _on_clicked(self, _button) -> None:
        if self._dragging:
            return  # that press was a drag, not a toggle
        self.on_press()

    def _on_key_pressed(self, controller, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.on_cancel()
            return True
        return False

    def show(self) -> None:
        if not self._window:
            return
        present_overlay(self._window, OverlayRole.PTT)
        # present_overlay pins the window on a 50 ms timer; apply our own
        # geometry once that has settled.
        from gi.repository import GLib

        GLib.timeout_add(250, self._apply_saved_geometry)

    def _apply_saved_geometry(self) -> bool:
        from gi.repository import GLib

        if self._x is None or self._y is None:
            self._default_position()
        elif self._monitor_geometry() is None:
            logger.info(
                "Saved PTT position (%s, %s) is off every monitor; "
                "falling back to the default spot",
                self._x, self._y,
            )
            self._edge = None
            self._default_position()
        self._clamp_to_monitor()
        self._update_edge()
        self._apply_position(tucked=not self._recording)
        return GLib.SOURCE_REMOVE

    def _clamp_to_monitor(self) -> None:
        """Keep the whole button inside its monitor's work area."""
        geo = self._monitor_geometry()
        if geo is None or self._x is None or self._y is None:
            return
        size = PTT_BUTTON_SIZE + 4
        self._x = max(geo.x, min(self._x, geo.x + geo.width - size))
        self._y = max(geo.y, min(self._y, geo.y + geo.height - size))

    def hide(self) -> None:
        if self._window:
            self._window.set_visible(False)

    def set_recording_state(self, is_recording: bool) -> None:
        """Update button visual state."""
        self._recording = is_recording
        if self._button:
            if is_recording:
                self._button.add_css_class("recording")
                self._button.set_label("⏹")  # ⏹
            else:
                self._button.remove_css_class("recording")
                self._button.set_label("\U0001f3a4")  # 🎤
        # Never leave the button half off-screen while it is the only
        # visible sign that dictation is live.
        self._apply_position(tucked=not is_recording)
