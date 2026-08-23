"""The on-screen touch keyboard window.

A non-focusing, always-on-top GTK4 window the user can drag and resize. Keys
inject into the focused application via xdotool. Geometry is persisted so it
reopens where the user left it.

Layout: a top strip (drag handle + size presets + close) over a grid of
keys. Each row is its own column-homogeneous Gtk.Grid so keys can have
proportional widths while every row still fills the full width.
"""

from __future__ import annotations

import json
import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from doubao_murmur.config import get_keyboard_config_path
from doubao_murmur.keyboard.injector import XdotoolInjector
from doubao_murmur.keyboard.key_button import KeyButton
from doubao_murmur.keyboard.layouts import (
    KIND_CHAR,
    KIND_LAYER,
    KIND_MOD,
    KIND_SPACER,
    LAYERS,
    split_row,
)
from doubao_murmur.keyboard.state import KeyboardState
from doubao_murmur.ui.windowing import (
    OverlayRole,
    apply_overlay_window_hints,
    present_overlay,
    x11_move,
    x11_move_resize,
    x11_pointer,
)

logger = logging.getLogger(__name__)

# Size presets as (width_fraction_of_monitor, height_px, css_class).
_SIZE_PRESETS = {
    "s": (0.60, 240, "size-s"),
    "m": (0.82, 300, "size-m"),
    "l": (0.96, 380, "size-l"),
}
_MIN_WIDTH = 300
_MIN_HEIGHT = 180

# Layout modes (cycled by the strip button). Defaults below come from the
# on-device thumb-reach calibration: each thumb comfortably covers only the
# outer ~20% of the screen, with the middle unreachable.
MODES = ("full", "split", "left", "right")
MODE_LABELS = {
    "full": "全键",
    "split": "分体",
    "left": "左手",
    "right": "右手",
}
# One-handed keyboard width as a fraction of the monitor. The thumb's reach is
# only ~20% of the screen, but cramming a full 10-column keyboard into that
# makes every key ~4mm wide — too small to hit. 0.35 keeps keys ~6mm (just
# tappable) while staying within a one-handed grip's sweep; the keyboard still
# docks to the reachable edge.
_ONEHAND_FRAC = 0.35
# Split mode: the wider of the two half-keyboards occupies this fraction of
# the screen at its edge; the unreachable middle is left empty.
_SPLIT_SIDE_FRAC = 0.22

_CSS = b"""
.keyboard-window {
    background: linear-gradient(180deg, #2b2d31, #212327);
    border-radius: 12px;
}
.keyboard-strip {
    background: rgba(0, 0, 0, 0.22);
    border-radius: 12px 12px 0 0;
    padding: 5px 8px;
}
.keyboard-title { color: rgba(255,255,255,0.4); font-size: 12px; }
.strip-btn {
    background: rgba(255,255,255,0.09);
    color: #e9e9ec;
    border-radius: 8px;
    border: none;
    min-width: 40px;
    min-height: 30px;
    padding: 0 10px;
    margin: 0 3px;
    font-size: 13px;
}
.strip-btn:hover { background: rgba(255,255,255,0.18); }
.strip-grip { font-size: 15px; }
.strip-close { background: rgba(205,66,66,0.85); color: #fff; }
.strip-close:hover { background: rgba(228,74,74,0.95); }

.key {
    background: linear-gradient(180deg, #4b4e54, #3b3e43);
    color: #f3f3f5;
    border-radius: 9px;
    border: 1px solid rgba(0,0,0,0.35);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.07),
                0 2px 3px rgba(0,0,0,0.30);
    margin: 3px;
    padding: 0;
    /* Override the theme's ~52px default button min so the grid can shrink
       to the narrow one-handed / split widths. */
    min-width: 0;
    font-size: 19px;
    font-weight: 500;
    min-height: 42px;
}
.key:hover { background: linear-gradient(180deg, #565a60, #45484e); }
.key:active {
    background: #6b7079;
    box-shadow: inset 0 2px 4px rgba(0,0,0,0.45);
}
.key-mod, .key-layer {
    background: linear-gradient(180deg, #3b4350, #323845);
    color: #c9d2de;
    font-size: 15px;
    font-weight: 600;
}
.key-mod:hover, .key-layer:hover {
    background: linear-gradient(180deg, #454e5d, #3a4150);
}
.key-mod.armed {
    background: linear-gradient(180deg, #4287e0, #316ec0);
    color: #fff;
}
.key-mod.locked {
    background: linear-gradient(180deg, #36a86d, #2a8757);
    color: #fff;
}
.key-space { background: linear-gradient(180deg, #45484e, #383b40); }
.keyboard-window.size-s .key { font-size: 16px; min-height: 32px; }
.keyboard-window.size-s .key-mod, .keyboard-window.size-s .key-layer {
    font-size: 13px;
}
.keyboard-window.size-m .key { font-size: 19px; min-height: 42px; }
.keyboard-window.size-l .key { font-size: 23px; min-height: 52px; }
.keyboard-window.size-l .key-mod, .keyboard-window.size-l .key-layer {
    font-size: 17px;
}
/* Compact: one-handed mode squeezes a full keyboard into ~20% of the screen
   (phone width). Tiny margins + smaller glyphs let the grid shrink that far,
   which the normal 3px margins would otherwise block. */
.keyboard-window.compact .key {
    margin: 1px;
    font-size: 14px;
    min-height: 30px;
    border-radius: 6px;
}
.keyboard-window.compact .key-mod, .keyboard-window.compact .key-layer {
    font-size: 11px;
}
.keyboard-window.compact .strip-btn {
    min-width: 22px;
    padding: 0 5px;
    margin: 1px;
    min-height: 26px;
    font-size: 11px;
}
"""

_css_installed = False


def _install_css() -> None:
    global _css_installed
    if _css_installed:
        return
    display = Gdk.Display.get_default()
    if display:
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _css_installed = True


class KeyboardWindow:
    """Draggable, resizable on-screen keyboard."""

    def __init__(self) -> None:
        self.state = KeyboardState(on_change=self._on_state_change)
        self._injector = XdotoolInjector()
        self._window: Gtk.Window | None = None
        self._grid_container: Gtk.Box | None = None
        self._buttons: list[KeyButton] = []
        self._rendered_layer: str | None = None
        self._visible = False
        self._size_class = "size-m"
        self._mode = "full"
        self._mode_btn: Gtk.Button | None = None
        self._size_buttons: list[Gtk.Button] = []

        # Tracked geometry (Python-side source of truth for drag/resize).
        self._x = 0
        self._y = 0
        self._width = 0
        self._height = 0
        # Drag/resize gesture anchors.
        self._anchor_pointer: tuple[int, int] | None = None
        self._anchor_geom: tuple[int, int, int, int] | None = None

        self._load_geometry()

    def available(self) -> bool:
        """Whether key injection is possible (xdotool present)."""
        return self._injector.available()

    # -- build --------------------------------------------------------------

    def _build(self) -> None:
        _install_css()
        self._window = Gtk.Window()
        self._window.set_title("Open Voice Input Linux Keyboard")
        self._window.set_decorated(False)
        self._window.set_resizable(True)
        # Belt-and-suspenders against focus theft on tap (the X11 input hint
        # in windowing.py is the primary mechanism).
        self._window.set_can_focus(False)
        self._window.set_focusable(False)
        self._window.add_css_class("keyboard-window")
        self._window.add_css_class(self._size_class)
        apply_overlay_window_hints(self._window, OverlayRole.KEYBOARD)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.append(self._build_strip())

        self._grid_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0
        )
        self._grid_container.set_homogeneous(True)
        self._grid_container.set_vexpand(True)
        self._grid_container.set_margin_start(3)
        self._grid_container.set_margin_end(3)
        self._grid_container.set_margin_bottom(3)
        root.append(self._grid_container)

        self._window.set_child(root)
        self._rebuild_keys()
        # Style the strip/keys for the restored mode (compact + hidden presets
        # for one-handed); geometry is asserted in show().
        self._sync_mode_styling()

    def _build_strip(self) -> Gtk.Widget:
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        strip.add_css_class("keyboard-strip")

        title = Gtk.Label(label="⌨  拖动移动")
        title.add_css_class("keyboard-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        # Ellipsize so the hint text never sets a large minimum width (the
        # one-handed window is only ~20% of the screen).
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_width_chars(0)
        # The whole expanding title area is the drag handle.
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_move_begin)
        drag.connect("drag-update", self._on_move_update)
        drag.connect("drag-end", self._on_drag_end)
        title.add_controller(drag)
        strip.append(title)

        # Layout-mode cycle button (full -> split -> left -> right).
        self._mode_btn = Gtk.Button(label=MODE_LABELS[self._mode])
        self._mode_btn.set_focusable(False)
        self._mode_btn.add_css_class("strip-btn")
        self._mode_btn.set_tooltip_text("切换布局：全键 / 分体 / 左手 / 右手")
        self._mode_btn.connect("clicked", self._on_mode_cycle)
        strip.append(self._mode_btn)

        self._size_buttons = []
        for key, label in (("s", "S"), ("m", "M"), ("l", "L")):
            btn = Gtk.Button(label=label)
            btn.set_focusable(False)
            btn.add_css_class("strip-btn")
            btn.connect("clicked", self._on_size_preset, key)
            strip.append(btn)
            self._size_buttons.append(btn)

        # Resize grip. A Label (not a Button) so its own click gesture does
        # not swallow the drag — same reason the title handle is a Label.
        grip = Gtk.Label(label="⤡")
        grip.add_css_class("strip-btn")
        grip.add_css_class("strip-grip")
        grip_drag = Gtk.GestureDrag()
        grip_drag.connect("drag-begin", self._on_resize_begin)
        grip_drag.connect("drag-update", self._on_resize_update)
        grip_drag.connect("drag-end", self._on_drag_end)
        grip.add_controller(grip_drag)
        strip.append(grip)

        close = Gtk.Button(label="✕")
        close.set_focusable(False)
        close.add_css_class("strip-btn")
        close.add_css_class("strip-close")
        close.connect("clicked", lambda _b: self.hide())
        strip.append(close)

        return strip

    def _rebuild_keys(self) -> None:
        if self._grid_container is None:
            return
        # Clear existing grid.
        child = self._grid_container.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._grid_container.remove(child)
            child = nxt
        self._buttons = []

        rows = LAYERS[self.state.layer]
        if self._mode == "split":
            content = self._build_split_grid(None, rows)
        else:
            # One shared grid for all rows: column-homogeneous so a key's
            # column span (its width in units) sets its proportion, and rows
            # of unequal total width end at different points — the keyboard
            # stagger, instead of a flush grid.
            content = Gtk.Grid()
            content.set_column_homogeneous(True)
            content.set_row_homogeneous(True)
            content.set_hexpand(True)
            content.set_vexpand(True)
            for r, row in enumerate(rows):
                col = 0
                for keydef in row:
                    col = self._attach_key(content, keydef, col, r)

        self._grid_container.append(content)
        self._rendered_layer = self.state.layer

    def _attach_key(self, grid, keydef, col: int, r: int) -> int:
        """Attach one key at (col, r); return the next column."""
        span = max(1, round(keydef.width))
        if keydef.kind != KIND_SPACER:
            kb = KeyButton(keydef, self)
            grid.attach(kb.widget, col, r, span, 1)
            self._buttons.append(kb)
        return col + span

    def _build_split_grid(self, _grid, rows) -> Gtk.Widget:
        """Split layout: two fixed-width half-keyboards hugging the left and
        right edges with a flexible spacer between them, so each half lands
        under a thumb and the unreachable middle just stretches.

        (A single column-homogeneous grid can't do this — its empty middle
        columns would each demand a key's min width and overflow the screen.)
        """
        geo = self._monitor_geometry()
        mw = geo[2] if geo else 1920
        half_px = max(_MIN_WIDTH // 2, int(mw * _SPLIT_SIDE_FRAC))

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        left_grid = self._new_half_grid(half_px)
        right_grid = self._new_half_grid(half_px)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)

        for r, row in enumerate(rows):
            left, right = split_row(row)
            col = 0
            for keydef in left:
                col = self._attach_key(left_grid, keydef, col, r)
            col = 0
            for keydef in right:
                col = self._attach_key(right_grid, keydef, col, r)

        hbox.append(left_grid)
        hbox.append(spacer)
        hbox.append(right_grid)
        return hbox

    @staticmethod
    def _new_half_grid(width_px: int) -> Gtk.Grid:
        g = Gtk.Grid()
        g.set_column_homogeneous(True)
        g.set_row_homogeneous(True)
        g.set_vexpand(True)
        g.set_hexpand(False)
        g.set_size_request(width_px, -1)
        return g

    # -- key dispatch -------------------------------------------------------

    def press_key(self, keydef) -> None:
        if keydef.kind == KIND_MOD:
            self.state.cycle_mod(keydef.mod)
        elif keydef.kind == KIND_LAYER:
            self.state.set_layer(keydef.target_layer)
        elif keydef.kind == KIND_CHAR and keydef.keysym:
            mods = self.state.active_mods()
            self._injector.send(keydef.keysym, mods)
            self.state.consume_after_char()

    def _on_state_change(self) -> None:
        # A layer switch needs a full rebuild; modifier/shift changes only
        # need label/active-class refreshes on the existing keys.
        if self._rendered_layer != self.state.layer or not self._buttons:
            self._rebuild_keys()
        else:
            for kb in self._buttons:
                kb.refresh()

    # -- move / resize ------------------------------------------------------

    def _on_move_begin(self, _gesture, _x, _y) -> None:
        self._anchor_pointer = x11_pointer()
        self._anchor_geom = (self._x, self._y, self._width, self._height)

    def _on_move_update(self, _gesture, _ox, _oy) -> None:
        if not self._anchor_pointer or not self._anchor_geom:
            return
        cur = x11_pointer()
        if cur is None:
            return
        dx = cur[0] - self._anchor_pointer[0]
        dy = cur[1] - self._anchor_pointer[1]
        gx, gy, _w, _h = self._anchor_geom
        self._x, self._y = self._clamp(gx + dx, gy + dy)
        if self._window:
            x11_move(self._window, self._x, self._y)

    def _on_resize_begin(self, _gesture, _x, _y) -> None:
        self._anchor_pointer = x11_pointer()
        self._anchor_geom = (self._x, self._y, self._width, self._height)

    def _on_resize_update(self, _gesture, _ox, _oy) -> None:
        if not self._anchor_pointer or not self._anchor_geom:
            return
        cur = x11_pointer()
        if cur is None:
            return
        dx = cur[0] - self._anchor_pointer[0]
        dy = cur[1] - self._anchor_pointer[1]
        _gx, _gy, gw, gh = self._anchor_geom
        self._width = max(_MIN_WIDTH, gw + dx)
        self._height = max(_MIN_HEIGHT, gh + dy)
        if self._window:
            x11_move_resize(
                self._window, self._x, self._y, self._width, self._height
            )

    def _on_drag_end(self, _gesture, _ox, _oy) -> None:
        self._anchor_pointer = None
        self._anchor_geom = None
        self._save_geometry()

    def _on_size_preset(self, _button, key: str) -> None:
        frac, height, css_class = _SIZE_PRESETS[key]
        geo = self._monitor_geometry()
        width = int(geo[2] * frac) if geo else max(_MIN_WIDTH, self._width)
        self._set_size_class(css_class)
        self._width = width
        self._height = height
        # Keep on-screen: clamp x so the keyboard stays within the monitor.
        if geo:
            mx, my, mw, mh = geo
            self._x = max(mx, min(self._x, mx + mw - width))
            self._y = max(my, min(self._y, my + mh - height))
        if self._window:
            x11_move_resize(
                self._window, self._x, self._y, self._width, self._height
            )
        self._save_geometry()

    def _set_size_class(self, css_class: str) -> None:
        if self._window:
            self._window.remove_css_class(self._size_class)
            self._window.add_css_class(css_class)
        self._size_class = css_class

    # -- layout mode --------------------------------------------------------

    def _on_mode_cycle(self, _button) -> None:
        idx = MODES.index(self._mode) if self._mode in MODES else 0
        self._mode = MODES[(idx + 1) % len(MODES)]
        if self._mode_btn:
            self._mode_btn.set_label(MODE_LABELS[self._mode])
        self._rebuild_keys()
        self._apply_mode_geometry()

    def _sync_mode_styling(self) -> None:
        """Apply the compact CSS class and size-preset button visibility for
        the current mode. One-handed modes use compact (smaller glyphs/margins
        so a full keyboard fits the narrow width) and hide the S/M/L presets,
        which are meaningless there. Idempotent — safe to call on every show()
        so a keyboard reopened in a one-handed mode is styled correctly."""
        compact = self._mode in ("left", "right")
        if self._window:
            if compact:
                self._window.add_css_class("compact")
            else:
                self._window.remove_css_class("compact")
        for btn in self._size_buttons:
            btn.set_visible(not compact)

    def _compute_mode_geometry(self) -> bool:
        """Recompute self._{x,y,width,height} from the current mode. Returns
        True if geometry was set. For one-handed/split modes the width is
        derived from the monitor (not the persisted value), so a stale saved
        width never survives a mode/monitor change."""
        geo = self._monitor_geometry()
        if not geo:
            return False
        mx, my, mw, mh = geo
        height = self._height or _SIZE_PRESETS["m"][1]

        if self._mode == "left":
            width = int(mw * _ONEHAND_FRAC)
            x = mx
        elif self._mode == "right":
            width = int(mw * _ONEHAND_FRAC)
            x = mx + mw - width
        elif self._mode == "split":
            width = mw
            x = mx
        else:  # full
            key = self._size_class.replace("size-", "")
            frac = _SIZE_PRESETS.get(key, _SIZE_PRESETS["m"])[0]
            width = int(mw * frac)
            x = mx + (mw - width) // 2

        self._width = max(_MIN_WIDTH, width)
        self._height = height
        self._x = x
        self._y = my + mh - height - 56
        return True

    def _apply_mode_geometry(self) -> None:
        """Resize/reposition the window for the current mode (full / split /
        one-handed left / one-handed right) and persist it."""
        self._sync_mode_styling()
        if not self._compute_mode_geometry():
            return
        if self._window:
            x11_move_resize(
                self._window, self._x, self._y, self._width, self._height
            )
        self._save_geometry()

    # -- geometry persistence ----------------------------------------------

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        """Keep the window on-screen so the drag strip stays reachable."""
        geo = self._monitor_geometry()
        if not geo:
            return x, y
        mx, my, mw, mh = geo
        # Always leave the top strip on-screen; allow the body to overhang
        # the bottom edge but never push the whole window past an edge.
        margin = 80
        x = max(mx - self._width + margin, min(x, mx + mw - margin))
        y = max(my, min(y, my + mh - margin))
        return x, y

    def _monitor_geometry(self) -> tuple[int, int, int, int] | None:
        display = Gdk.Display.get_default()
        if not display:
            return None
        monitors = display.get_monitors()
        monitor = monitors.get_item(0) if monitors.get_n_items() else None
        if monitor is None:
            return None
        g = monitor.get_geometry()
        return (g.x, g.y, g.width, g.height)

    def _default_geometry(self) -> None:
        geo = self._monitor_geometry()
        if geo:
            mx, my, mw, mh = geo
            self._width = int(mw * _SIZE_PRESETS["m"][0])
            self._height = _SIZE_PRESETS["m"][1]
            self._x = mx + (mw - self._width) // 2
            self._y = my + mh - self._height - 56
        else:
            self._width, self._height = 900, 300
            self._x, self._y = 80, 480

    def _load_geometry(self) -> None:
        path = get_keyboard_config_path()
        try:
            data = json.loads(path.read_text())
            self._x = int(data["x"])
            self._y = int(data["y"])
            self._width = max(_MIN_WIDTH, int(data["width"]))
            self._height = max(_MIN_HEIGHT, int(data["height"]))
            self._size_class = data.get("size_class", "size-m")
            mode = data.get("mode", "full")
            self._mode = mode if mode in MODES else "full"
            logger.info("Loaded keyboard geometry: %s", data)
        except Exception:
            self._default_geometry()

    def _save_geometry(self) -> None:
        path = get_keyboard_config_path()
        data = {
            "x": self._x,
            "y": self._y,
            "width": self._width,
            "height": self._height,
            "size_class": self._size_class,
            "mode": self._mode,
        }
        try:
            path.write_text(json.dumps(data))
        except Exception as e:
            logger.warning("Could not save keyboard geometry: %s", e)

    # -- visibility ---------------------------------------------------------

    def show(self) -> None:
        if self._window is None:
            self._build()
        if self._width <= 0 or self._height <= 0:
            self._default_geometry()
        # Derive width from the mode for one-handed/split so a stale saved
        # width (e.g. from an older _ONEHAND_FRAC or a different monitor) is
        # not reapplied. Full mode keeps the user's persisted size.
        if self._mode in ("left", "right", "split"):
            self._compute_mode_geometry()
        present_overlay(self._window, OverlayRole.KEYBOARD)
        self._visible = True
        # Apply saved geometry once the surface is mapped (mapping is async),
        # then re-assert a few times: KWin places the window with its own
        # policy right after map and would otherwise win the race.
        state = {"tries": 0, "applied": 0}
        GLib.timeout_add(60, self._apply_geometry, state)

    def _apply_geometry(self, state: dict) -> bool:
        if self._window is None:
            return GLib.SOURCE_REMOVE
        if not self._window.get_width() or not self._window.get_height():
            state["tries"] += 1
            return (
                GLib.SOURCE_CONTINUE if state["tries"] < 20
                else GLib.SOURCE_REMOVE
            )
        x11_move_resize(
            self._window, self._x, self._y, self._width, self._height
        )
        state["applied"] += 1
        # A handful of re-applies over ~0.5s settles position against KWin.
        return GLib.SOURCE_CONTINUE if state["applied"] < 5 else GLib.SOURCE_REMOVE

    def hide(self) -> None:
        if self._window:
            self._window.set_visible(False)
        self._visible = False

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    @property
    def is_visible(self) -> bool:
        return self._visible
