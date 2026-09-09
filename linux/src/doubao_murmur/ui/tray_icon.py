"""Minimal tray and status window for the controller-only compatibility UI."""

# gi.require_version() must precede gi.repository imports.
# ruff: noqa: E402

from __future__ import annotations

import logging

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk

from doubao_murmur.app_state import (
    CLIPBOARD_ARMED_NOTICE,
    CLIPBOARD_READY_NOTICE,
    AppState,
    RecordingState,
    StatusNotice,
)
from doubao_murmur.ui.sni_tray import SniTray

logger = logging.getLogger(__name__)

_APP_ICON = "com.doubao.Murmur"
_FALLBACK_ICON = "audio-input-microphone"
_VISIBLE_APP_TITLE = "Open Voice Input Linux"
_VISIBLE_APP_TOOLTIP = "Open Voice Input Linux · 语音输入"
_STATE_LABELS = {
    RecordingState.IDLE: "状态：空闲",
    RecordingState.STARTING: "状态：正在启动语音输入",
    RecordingState.RECORDING: "状态：正在录音",
    RecordingState.STOPPING: "状态：正在等待最终结果",
    RecordingState.OBSERVING: "状态：已提交，正在观察原位纠错",
}
_NOTICE_LABELS = {
    StatusNotice.CLIPBOARD_ARMED: f"状态：📋 {CLIPBOARD_ARMED_NOTICE}",
    StatusNotice.CLIPBOARD_READY: f"状态：✓ {CLIPBOARD_READY_NOTICE}",
}


class TrayIcon:
    """StatusNotifierItem plus a provider-free local status window."""

    def __init__(
        self,
        *,
        app_state: AppState,
        on_quit_clicked,
        on_help_clicked=None,
    ) -> None:
        self.app_state = app_state
        self._on_quit_clicked = on_quit_clicked
        self._on_help_clicked = on_help_clicked
        self._sni: SniTray | None = None
        self._window: Gtk.Window | None = None
        self._status_label: Gtk.Label | None = None

    def start(self) -> None:
        self._build_control_window()
        self._start_sni()
        self.app_state.connect("recording-state-changed", lambda *_: self._refresh())
        self.app_state.connect("error-message-changed", lambda *_: self._refresh())
        self.app_state.connect("status-notice-changed", lambda *_: self._refresh())
        self._refresh()

    def stop(self) -> None:
        if self._sni:
            self._sni.stop()
            self._sni = None
        if self._window:
            self._window.destroy()
            self._window = None

    def _start_sni(self) -> None:
        try:
            tray = SniTray(
                item_id="doubao-murmur",
                title=_VISIBLE_APP_TITLE,
                tooltip=_VISIBLE_APP_TOOLTIP,
                icon_name=self._pick_icon(),
                on_activate=self.show_window,
            )
            if tray.start():
                self._sni = tray
        except Exception:
            logger.exception("Tray icon unavailable; status window only")

    @staticmethod
    def _pick_icon() -> str:
        try:
            display = Gdk.Display.get_default()
            if display:
                theme = Gtk.IconTheme.get_for_display(display)
                if theme.has_icon(_APP_ICON):
                    return _APP_ICON
        except Exception:
            pass
        return _FALLBACK_ICON

    def _menu_items(self) -> list[dict | None]:
        items: list[dict | None] = [
            {"label": self._status_text(), "enabled": False},
            None,
            {"label": "状态", "callback": self.show_window},
        ]
        if self._on_help_clicked:
            items.append({"label": "使用帮助", "callback": self._on_help_clicked})
        items.extend(
            [
                None,
                {
                    "label": "完全退出（停用语音快捷键）",
                    "callback": self._on_quit_clicked,
                },
            ]
        )
        return items

    def _build_control_window(self) -> None:
        self._window = Gtk.Window()
        self._window.set_title(_VISIBLE_APP_TITLE)
        self._window.set_default_size(400, 230)
        self._window.set_resizable(False)
        self._window.connect("close-request", self._on_control_close)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        self._status_label = Gtk.Label(xalign=0, wrap=True)
        box.append(self._status_label)

        disclosure = Gtk.Label(
            label=(
                "此兼容界面只把右 Alt、ESC 和悬浮按钮转发给独立语音服务。"
                "它不会读取 API Key，也没有麦克风或供应商网络权限。"
                "关闭此窗口会保留快捷键和悬浮麦克风。"
                "完全退出会停用它们；重新打开 Open Voice Input Linux 可恢复。"
            ),
            xalign=0,
            wrap=True,
        )
        box.append(disclosure)

        if self._on_help_clicked:
            help_button = Gtk.Button(label="使用帮助")
            help_button.connect("clicked", lambda _: self._on_help_clicked())
            box.append(help_button)

        quit_button = Gtk.Button(label="完全退出（停用语音快捷键）")
        quit_button.connect("clicked", lambda _: self._on_quit_clicked())
        box.append(quit_button)
        self._window.set_child(box)

    def _status_text(self) -> str:
        if self.app_state.error_message:
            return f"状态：⚠ {self.app_state.error_message}"
        if self.app_state.recording_state is not RecordingState.IDLE:
            return _STATE_LABELS.get(self.app_state.recording_state, "状态：未知")
        notice = _NOTICE_LABELS.get(self.app_state.status_notice)
        if notice is not None:
            return notice
        return _STATE_LABELS.get(self.app_state.recording_state, "状态：未知")

    def _refresh(self) -> None:
        text = self._status_text()
        if self._status_label:
            self._status_label.set_text(text)
        if self._sni:
            self._sni.set_menu(self._menu_items())
            self._sni.set_tooltip(text)

    def show_window(self) -> None:
        if self._window:
            self._window.present()

    def _on_control_close(self, _window) -> bool:
        self._window.set_visible(False)
        return True
