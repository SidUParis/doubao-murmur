"""System tray / control surface.

GTK4 removed GtkStatusIcon and AyatanaAppIndicator3 needs GTK3 menus, so
the tray icon is implemented over raw DBus (StatusNotifierItem — see
sni_tray.py), which KDE Plasma and most other trays speak natively.

A small control window backs it up: it is the click target of the tray
icon, the fallback UI when no StatusNotifierWatcher is running (e.g. stock
GNOME), and what a second app launch brings up.
"""

# gi.require_version() must run before importing gi.repository.
# ruff: noqa: E402

from __future__ import annotations

import logging

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk

from doubao_murmur.app_state import AppState, LoginStatus, RecordingState
from doubao_murmur.config import (
    MAX_PERSONAL_VOCABULARY_TERM_LENGTH,
    MAX_PERSONAL_VOCABULARY_TERMS,
    has_volcengine_api_key,
    load_personal_vocabulary,
    save_personal_vocabulary,
    save_volcengine_api_key,
)
from doubao_murmur.ui.sni_tray import SniTray

logger = logging.getLogger(__name__)

_APP_ICON = "com.doubao.Murmur"
_FALLBACK_ICON = "audio-input-microphone"
_VISIBLE_APP_TITLE = "Open Voice Input Linux"
_VISIBLE_APP_TOOLTIP = "Open Voice Input Linux · 语音输入"
_PERSONAL_VOCABULARY_DISCLOSURE = (
    "一行一个术语。使用火山引擎时，词表会随每次语音请求发送给火山引擎；"
    "不会读取剪贴板或输入历史。"
)


class TrayIcon:
    """System tray icon (SNI) plus control window."""

    def __init__(
        self,
        app_state: AppState,
        on_login_clicked,
        on_logout_clicked,
        on_quit_clicked,
        on_help_clicked=None,
        on_keyboard_clicked=None,
        uses_doubao_login: bool = True,
        on_backend_reload=None,
    ) -> None:
        self.app_state = app_state
        self._on_login_clicked = on_login_clicked
        self._on_logout_clicked = on_logout_clicked
        self._on_quit_clicked = on_quit_clicked
        self._on_help_clicked = on_help_clicked
        self._on_keyboard_clicked = on_keyboard_clicked
        self._uses_doubao_login = uses_doubao_login
        self._on_backend_reload = on_backend_reload
        self._sni: SniTray | None = None
        self._window: Gtk.Window | None = None
        self._status_label: Gtk.Label | None = None
        self._primary_button: Gtk.Button | None = None
        self._volcengine_key_entry: Gtk.PasswordEntry | None = None
        self._volcengine_message_label: Gtk.Label | None = None
        self._personal_vocabulary_view: Gtk.TextView | None = None
        self._personal_vocabulary_message_label: Gtk.Label | None = None

    def start(self) -> None:
        """Create the control window and (if possible) the tray icon."""
        self._build_control_window()
        self._start_sni()
        self.app_state.connect(
            "login-status-changed", lambda *_: self._refresh()
        )
        self._refresh()
        # Start minimized: only pop up when user action is required
        # (not logged in). Re-activating the app (launching it again)
        # or clicking the tray icon presents the window.
        if self.app_state.login_status != LoginStatus.LOGGED_IN:
            self._window.present()

    # -- tray icon -----------------------------------------------------------

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
            logger.exception("Tray icon unavailable; control window only")

    @staticmethod
    def _pick_icon() -> str:
        """App icon if installed (Flatpak/system), generic mic otherwise."""
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
        logged_in = self.app_state.login_status == LoginStatus.LOGGED_IN
        status_map = {
            LoginStatus.CHECKING: "⏳ 检查中...",
            LoginStatus.LOGGED_IN: "✅ 已登录",
            LoginStatus.NOT_LOGGED_IN: "❌ 未登录",
        }
        items: list[dict | None] = [
            {
                "label": (
                    status_map.get(self.app_state.login_status, "⏳")
                    if self._uses_doubao_login
                    else "✅ 语音后端已配置"
                ),
                "enabled": False,
            },
            None,
        ]
        if self._uses_doubao_login:
            items.append(
                {
                    "label": "退出登录" if logged_in else "登录豆包",
                    "callback": (
                        self._on_logout_clicked
                        if logged_in
                        else self._on_login_clicked
                    ),
                }
            )
        items.append({"label": "控制面板", "callback": self.show_window})
        if self._on_keyboard_clicked:
            items.append(
                {"label": "⌨ 软键盘", "callback": self._on_keyboard_clicked}
            )
        if self._on_help_clicked:
            items.append({"label": "使用帮助", "callback": self._on_help_clicked})
        items.append(None)
        items.append({"label": "退出", "callback": self._on_quit_clicked})
        return items

    # -- control window --------------------------------------------------------

    def _build_control_window(self) -> None:
        self._window = Gtk.Window()
        self._window.set_title(_VISIBLE_APP_TITLE)
        self._window.set_default_size(480, 680)
        self._window.set_resizable(True)
        self._window.connect("close-request", self._on_control_close)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0)
        box.append(self._status_label)

        self._primary_button = Gtk.Button()
        self._primary_button.connect("clicked", self._on_primary_clicked)
        box.append(self._primary_button)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        volcengine_title = Gtk.Label(label="火山引擎语音识别")
        volcengine_title.set_xalign(0)
        volcengine_title.add_css_class("heading")
        box.append(volcengine_title)

        volcengine_hint = Gtk.Label(
            label=(
                "先在自己的火山引擎账号开通语音识别大模型服务；"
                "然后输入 API Key，即可启用流式识别、二遍识别和自动标点。"
            )
        )
        volcengine_hint.set_xalign(0)
        volcengine_hint.set_wrap(True)
        box.append(volcengine_hint)

        self._volcengine_key_entry = Gtk.PasswordEntry()
        self._volcengine_key_entry.set_property(
            "placeholder-text", "Volcengine API Key"
        )
        self._volcengine_key_entry.set_show_peek_icon(True)
        box.append(self._volcengine_key_entry)

        save_volcengine_button = Gtk.Button(label="保存并启用火山引擎")
        save_volcengine_button.connect(
            "clicked", self._on_save_volcengine_clicked
        )
        box.append(save_volcengine_button)

        self._volcengine_message_label = Gtk.Label()
        self._volcengine_message_label.set_xalign(0)
        self._volcengine_message_label.set_wrap(True)
        self._volcengine_message_label.set_text(
            "API Key 已保存；重新输入可替换。"
            if has_volcengine_api_key()
            else "尚未保存火山引擎 API Key。"
        )
        box.append(self._volcengine_message_label)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        vocabulary_title = Gtk.Label(label="个人词表（可选）")
        vocabulary_title.set_xalign(0)
        vocabulary_title.add_css_class("heading")
        box.append(vocabulary_title)

        vocabulary_disclosure = Gtk.Label(
            label=_PERSONAL_VOCABULARY_DISCLOSURE
        )
        vocabulary_disclosure.set_xalign(0)
        vocabulary_disclosure.set_wrap(True)
        box.append(vocabulary_disclosure)

        vocabulary_scroll = Gtk.ScrolledWindow()
        vocabulary_scroll.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        vocabulary_scroll.set_min_content_height(90)
        self._personal_vocabulary_view = Gtk.TextView()
        self._personal_vocabulary_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        terms = load_personal_vocabulary()
        self._personal_vocabulary_view.get_buffer().set_text("\n".join(terms))
        vocabulary_scroll.set_child(self._personal_vocabulary_view)
        box.append(vocabulary_scroll)

        save_vocabulary_button = Gtk.Button(label="保存个人词表")
        save_vocabulary_button.connect(
            "clicked", self._on_save_personal_vocabulary_clicked
        )
        box.append(save_vocabulary_button)

        self._personal_vocabulary_message_label = Gtk.Label()
        self._personal_vocabulary_message_label.set_xalign(0)
        self._personal_vocabulary_message_label.set_wrap(True)
        self._personal_vocabulary_message_label.set_text(
            f"已保存 {len(terms)} 个术语。" if terms else "尚未保存个人词表。"
        )
        box.append(self._personal_vocabulary_message_label)

        if self._on_keyboard_clicked:
            keyboard_button = Gtk.Button(label="⌨ 软键盘")
            keyboard_button.connect(
                "clicked", lambda _: self._on_keyboard_clicked()
            )
            box.append(keyboard_button)

        if self._on_help_clicked:
            help_button = Gtk.Button(label="使用帮助")
            help_button.connect("clicked", lambda _: self._on_help_clicked())
            box.append(help_button)

        quit_button = Gtk.Button(label="退出")
        quit_button.connect("clicked", lambda _: self._on_quit_clicked())
        box.append(quit_button)

        self._window.set_child(box)

    def _refresh(self) -> None:
        """Sync tray menu and control window with the current state."""
        if self._sni:
            self._sni.set_menu(self._menu_items())

        status_map = {
            LoginStatus.CHECKING: "状态：检查中...",
            LoginStatus.LOGGED_IN: "状态：已登录",
            LoginStatus.NOT_LOGGED_IN: "状态：未登录",
        }
        if self._status_label:
            status = (
                status_map.get(
                    self.app_state.login_status, "状态：检查中..."
                )
                if self._uses_doubao_login
                else "状态：语音后端已配置"
            )
            self._status_label.set_text(status)
        if self._primary_button:
            self._primary_button.set_visible(self._uses_doubao_login)
            if self._uses_doubao_login:
                if self.app_state.login_status == LoginStatus.LOGGED_IN:
                    self._primary_button.set_label("退出登录")
                else:
                    self._primary_button.set_label("登录豆包")

    def set_uses_doubao_login(self, enabled: bool) -> None:
        """Refresh login controls after an in-process backend switch."""
        self._uses_doubao_login = bool(enabled)
        self._refresh()

    def _on_primary_clicked(self, _button) -> None:
        if not self._uses_doubao_login:
            return
        if self.app_state.login_status == LoginStatus.LOGGED_IN:
            self._on_logout_clicked()
        else:
            self._on_login_clicked()

    def _on_save_volcengine_clicked(self, _button) -> None:
        """Persist the masked field without ever logging its contents."""
        entry = self._volcengine_key_entry
        status = self._volcengine_message_label
        if entry is None:
            return

        try:
            save_volcengine_api_key(entry.get_text())
        except ValueError:
            if status:
                status.set_text("请输入有效的火山引擎 API Key。")
            return
        except Exception as error:
            # Some transport/filesystem exceptions can include call arguments
            # in their text.  Report only the class, never the submitted key.
            logger.error(
                "Could not save Volcengine settings (%s)",
                error.__class__.__name__,
            )
            if status:
                status.set_text("保存失败，请检查配置目录权限后重试。")
            return

        entry.set_text("")
        reloaded = False
        if self._on_backend_reload:
            try:
                reloaded = bool(self._on_backend_reload())
            except Exception as error:
                logger.error(
                    "Could not reload Volcengine backend (%s)",
                    error.__class__.__name__,
                )
        if status:
            if reloaded:
                status.set_text("已保存并启用；下一次录音生效。")
            elif self.app_state.recording_state != RecordingState.IDLE:
                status.set_text(
                    "API Key 已安全保存；请停止当前录音后再次保存，"
                    "或重启应用生效。"
                )
            else:
                status.set_text(
                    "API Key 已安全保存；后端重载失败，请重启应用生效。"
                )

    def _on_save_personal_vocabulary_clicked(self, _button) -> None:
        """Save only terms the user explicitly entered in the control panel."""
        view = self._personal_vocabulary_view
        status = self._personal_vocabulary_message_label
        if view is None:
            return

        text_buffer = view.get_buffer()
        text = text_buffer.get_text(
            text_buffer.get_start_iter(),
            text_buffer.get_end_iter(),
            True,
        )
        try:
            saved = save_personal_vocabulary(text)
        except ValueError:
            if status:
                status.set_text(
                    "词表格式无效：最多 "
                    f"{MAX_PERSONAL_VOCABULARY_TERMS} 项，每项不超过 "
                    f"{MAX_PERSONAL_VOCABULARY_TERM_LENGTH} 个字符。"
                )
            return
        except Exception as error:
            # As with credentials, never interpolate user text or an exception
            # whose message could repeat that text.
            logger.error(
                "Could not save personal vocabulary (%s)",
                error.__class__.__name__,
            )
            if status:
                status.set_text("词表保存失败，请检查配置目录权限后重试。")
            return

        text_buffer.set_text("\n".join(saved))
        if status:
            status.set_text(
                f"已保存 {len(saved)} 个术语；下一次录音生效。"
                if saved
                else "个人词表已清空；下一次录音生效。"
            )

    def show_window(self) -> None:
        """Present the control window (tray click / app re-activation)."""
        if self._window:
            self._window.present()

    def _on_control_close(self, _window) -> bool:
        # Hide instead of quit — the app keeps running for the hotkey.
        # Quit via the tray menu or the window's 退出 button.
        self._window.set_visible(False)
        return True
