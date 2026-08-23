"""Main GTK Application.

Orchestrates all components and manages the application lifecycle.
"""

from __future__ import annotations

import logging
import uuid

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from doubao_murmur.app_state import AppState, LoginStatus, RecordingState
from doubao_murmur.hotkey.evdev_listener import EvdevListener
from doubao_murmur.hotkey.manager import HotkeyManager
from doubao_murmur.hotkey.overlay_button import OverlayButton
from doubao_murmur.hotkey.x11_listener import X11KeyListener
from doubao_murmur.keyboard.keyboard_window import KeyboardWindow
from doubao_murmur.params_store import ParamsStore
from doubao_murmur.paste.paste_helper import PasteHelper, PasteTarget
from doubao_murmur.preedit_client import AcquireResult, PreeditClient
from doubao_murmur.transcription import (
    TranscriptionManager,
    backend_needs_doubao_login,
)
from doubao_murmur.ui.login_window import LoginWindow
from doubao_murmur.ui.tray_icon import TrayIcon

logger = logging.getLogger(__name__)


class DoubaoMurmurApp(Gtk.Application):
    """Main GTK Application."""

    def __init__(self) -> None:
        super().__init__(
            application_id="com.doubao.Murmur",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.app_state = AppState()
        self.login_window: LoginWindow | None = None
        self.tray_icon: TrayIcon | None = None
        self.hotkey_manager: HotkeyManager | None = None
        self.transcription_manager: TranscriptionManager | None = None
        self.ptt_button: OverlayButton | None = None
        self.keyboard: KeyboardWindow | None = None
        self.preedit_client = PreeditClient()
        self._delivery_mode = "none"
        self._preedit_utterance_id: str | None = None
        self._preedit_revision = 0
        self._paste_target: PasteTarget | None = None
        self._setup_done = False

    def do_activate(self):
        if self._setup_done:
            # Second launch of the single-instance app: surface the
            # control window (it starts hidden when logged in).
            if self.tray_icon:
                self.tray_icon.show_window()
            return
        self._setup_done = True
        # None of our windows are Gtk.ApplicationWindows, so hold the
        # application alive explicitly; released in _quit().
        self.hold()
        self._setup_components()

    def _setup_components(self) -> None:
        # 1. Establish login state. A backend that carries its own
        # credentials never needs the doubao WebView, so treat it as
        # logged in and keep the tray/PTT button usable.
        if not backend_needs_doubao_login():
            self.app_state.login_status = LoginStatus.LOGGED_IN
            logger.info("Non-doubao backend configured; skipping login")
        elif ParamsStore.has_saved():
            self.app_state.login_status = LoginStatus.LOGGED_IN
            logger.info("Cached params found, skipping WebView")
        else:
            self.app_state.login_status = LoginStatus.NOT_LOGGED_IN

        # 2. Create transcription manager. The legacy text overlay is
        # deliberately not constructed: this sidecar shows state on the PTT
        # button and commits only the authoritative final result.
        self.transcription_manager = TranscriptionManager(self.app_state)
        self.transcription_manager.on_auth_expired = self._on_auth_expired
        self.transcription_manager.on_show_login = self._show_login
        self.transcription_manager.on_paste = self._do_paste
        self.transcription_manager.on_overlay_update = self._on_transcription_partial
        self.transcription_manager.on_params_needed = self._extract_params
        self.transcription_manager.on_cancel_enabled_changed = (
            self._on_cancel_enabled_changed
        )
        # 3. Create PTT button
        self.ptt_button = OverlayButton(
            on_press=self._handle_toggle,
            on_cancel=self._handle_cancel,
        )
        self.ptt_button.create()

        # 4. Create hotkey manager
        self.hotkey_manager = HotkeyManager()
        self.hotkey_manager.on_toggle = self._handle_toggle
        self.hotkey_manager.on_cancel = self._handle_cancel
        self.hotkey_manager.on_keyboard = self._toggle_keyboard

        # Prefer the X11 listener: it sees both physical keys and
        # XTEST-injected ones (Steam Input desktop layouts inject
        # controller-mapped keys via XTEST, invisible to evdev).
        # evdev remains the fallback for non-X11 sessions.
        x11 = None
        evdev = None
        if X11KeyListener.is_available():
            x11 = X11KeyListener(
                on_toggle=self.hotkey_manager.trigger_toggle,
                on_escape=self.hotkey_manager.trigger_cancel,
                on_keyboard=self.hotkey_manager.trigger_keyboard,
            )
        elif EvdevListener.is_available():
            evdev = EvdevListener(
                on_toggle=self.hotkey_manager.trigger_toggle,
                on_escape=self.hotkey_manager.trigger_cancel,
            )

        self.hotkey_manager.start(
            overlay_button=self.ptt_button,
            evdev_listener=evdev,
            x11_listener=x11,
        )

        # Wire PTT button to recording state
        self.app_state.connect(
            "recording-state-changed", self._on_recording_state_changed
        )
        self.app_state.connect("error-message-changed", self._on_error_message_changed)

        # Keep the button docked like an input-method widget: it is small,
        # dims while idle and tucks against a screen edge, so it no longer
        # needs to be hidden between dictations.
        if self.app_state.login_status == LoginStatus.LOGGED_IN:
            self.ptt_button.show()

        self.app_state.connect("login-status-changed", self._on_login_status_changed)

        # 5. Create on-screen keyboard (lazily shown via tray)
        self.keyboard = KeyboardWindow()

        # 6. Create tray icon
        self.tray_icon = TrayIcon(
            app_state=self.app_state,
            on_login_clicked=self._show_login,
            on_logout_clicked=self._do_logout,
            on_quit_clicked=self._quit,
            on_help_clicked=self._show_help,
            on_keyboard_clicked=self._toggle_keyboard,
            uses_doubao_login=backend_needs_doubao_login(),
            on_backend_reload=self._reload_backend_after_settings,
        )
        self.tray_icon.start()

        logger.info("All components initialized")

    def _reload_backend_after_settings(self) -> bool:
        """Apply saved backend settings when no recording is in flight."""
        manager = self.transcription_manager
        if manager is None or not manager.reload_backend():
            return False

        uses_doubao_login = manager.backend_name == "doubao"
        if self.tray_icon:
            self.tray_icon.set_uses_doubao_login(uses_doubao_login)

        login_status = LoginStatus.LOGGED_IN
        if uses_doubao_login and not ParamsStore.has_saved():
            login_status = LoginStatus.NOT_LOGGED_IN
        self.app_state.login_status = login_status
        if self.ptt_button:
            if login_status == LoginStatus.LOGGED_IN:
                self.ptt_button.show()
            else:
                self.ptt_button.hide()
        return True

    def _toggle_keyboard(self) -> None:
        if not self.keyboard:
            return
        if not self.keyboard.available():
            dialog = Gtk.MessageDialog(
                transient_for=None,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="软键盘不可用",
            )
            dialog.set_property(
                "secondary-text",
                "需要 xdotool 才能把按键输入到其他窗口。\nsudo pacman -S xdotool",
            )
            dialog.connect("response", lambda d, _: d.destroy())
            dialog.present()
            return
        self.keyboard.toggle()

    def _show_login(self) -> None:
        if not LoginWindow.is_available():
            dialog = Gtk.MessageDialog(
                transient_for=None,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="WebKitGTK 不可用",
            )
            dialog.set_property(
                "secondary-text",
                "请安装 webkitgtk-6.0 以使用登录功能。\nsudo pacman -S webkitgtk-6.0",
            )
            dialog.connect("response", lambda d, _: d.destroy())
            dialog.present()
            return

        if not self.login_window:
            self.login_window = LoginWindow(self.app_state)
            self.login_window._on_login_status_change = self._on_login_detected
            self.login_window.load()
        self.login_window.show()

    def _on_login_detected(self, status: str, nickname: str | None) -> None:
        if status == "loggedIn":
            self.app_state.login_status = LoginStatus.LOGGED_IN
            logger.info("Logged in as: %s", nickname)
            self._extract_save_and_destroy_webview()
        else:
            self.app_state.login_status = LoginStatus.NOT_LOGGED_IN

    def _extract_save_and_destroy_webview(self) -> None:
        """Extract params, save, destroy WebView."""

        def on_params(params):
            if params:
                ParamsStore.save(params)
            if self.login_window:
                self.login_window.hide()
                self.login_window.destroy()
                self.login_window = None

        # Delay 1s for cookies to settle
        GLib.timeout_add(
            1000,
            lambda: (
                self.login_window.extract_params_async(on_params)
                if self.login_window
                else None,
                GLib.SOURCE_REMOVE,
            )[1],
        )

    def _extract_params(self, callback) -> None:
        """Called by TranscriptionManager when params are needed."""
        if self.login_window and self.login_window.is_active:
            self.login_window.extract_params_async(callback)
        else:
            callback(None)

    def _handle_toggle(self) -> None:
        """Choose an inline-preedit or safe legacy route, then toggle."""
        if not self.transcription_manager:
            return
        starting = self.app_state.recording_state == RecordingState.IDLE
        if starting:
            utterance_id = f"voice-{uuid.uuid4().hex}"
            acquisition = self.preedit_client.acquire_result(utterance_id)
            if acquisition is AcquireResult.REJECTED:
                if self.preedit_client.restore_pending:
                    self._show_restore_failed_status()
                else:
                    self._show_preedit_rejected_status()
                return
            if acquisition is AcquireResult.ACQUIRED:
                self._delivery_mode = "preedit"
                self._preedit_utterance_id = utterance_id
                self._preedit_revision = 0
                self._paste_target = None
            else:
                # The development engine is optional. Only a genuinely absent
                # service may use the window-bound clipboard fallback; a
                # private/password-field rejection must never reach this path.
                self._delivery_mode = "paste"
                self._paste_target = PasteHelper.capture_target()
        self.transcription_manager.handle_toggle()
        # Login/configuration errors can reject the start without a state
        # transition. Do not retain that attempted target for a later session.
        if starting and self.app_state.recording_state == RecordingState.IDLE:
            self._cancel_preedit_route()
            self._paste_target = None

    def _on_transcription_partial(self, text: str) -> None:
        """Replace the cumulative IBus preedit with the latest ASR draft."""
        utterance_id = self._preedit_utterance_id
        if self._delivery_mode != "preedit" or utterance_id is None:
            return
        self._preedit_revision += 1
        if self.preedit_client.partial(utterance_id, self._preedit_revision, text):
            return

        # A focus change or stale input context invalidates the engine route.
        # Stop ASR and restore the previous engine; never reinterpret this as a
        # paste request because some partial text may already have been shown.
        self._cancel_preedit_route()
        self._paste_target = None
        if self.transcription_manager:
            self.transcription_manager.handle_cancel()
        self._show_preedit_lost_status()

    def _do_paste(self, text: str) -> None:
        """Commit through IBus, or use the window-bound legacy fallback."""
        utterance_id = self._preedit_utterance_id
        if self._delivery_mode == "preedit" and utterance_id is not None:
            self._preedit_revision += 1
            accepted = self.preedit_client.final(
                utterance_id, self._preedit_revision, text
            )
            restore_pending = self.preedit_client.restore_pending
            self._preedit_utterance_id = None
            self._preedit_revision = 0
            self._delivery_mode = "none"
            self._paste_target = None
            if restore_pending:
                GLib.idle_add(self._show_restore_failed_status)
            elif not accepted and text:
                # Once preedit was acquired, never reinterpret its final frame
                # as clipboard input: the focus-bound context may have failed
                # precisely because the user moved to a sensitive field.
                GLib.idle_add(self._show_preedit_delivery_failed_status)
            return

        if self._delivery_mode != "paste":
            logger.info(
                "Ignoring a late transcription result (%d characters)",
                len(text),
            )
            return

        target = self._paste_target
        self._delivery_mode = "none"
        self._paste_target = None
        pasted = PasteHelper.copy_and_paste(text, target=target)
        if not pasted and text:
            GLib.idle_add(self._show_copied_only_status)

    def _show_copied_only_status(self) -> bool:
        if self.ptt_button:
            self.ptt_button.set_error("输入焦点已改变；识别结果已复制到剪贴板")
        return GLib.SOURCE_REMOVE

    def _show_preedit_rejected_status(self) -> None:
        if self.ptt_button:
            self.ptt_button.set_error(
                "当前输入框不允许实时语音输入（可能是密码或私密字段）"
            )

    def _show_preedit_lost_status(self) -> None:
        if self.ptt_button:
            self.ptt_button.set_error("输入焦点已改变；本次语音输入已取消")

    def _show_preedit_delivery_failed_status(self) -> bool:
        if self.ptt_button:
            self.ptt_button.set_error("实时输入上下文已失效；结果未提交，请重试")
        return GLib.SOURCE_REMOVE

    def _show_restore_failed_status(self) -> bool:
        if self.ptt_button:
            self.ptt_button.set_error("原输入法自动恢复失败；请手动切回后再试")
        return GLib.SOURCE_REMOVE

    def _cancel_preedit_route(self) -> None:
        utterance_id = self._preedit_utterance_id
        self._delivery_mode = "none"
        self._preedit_utterance_id = None
        self._preedit_revision = 0
        if utterance_id is not None:
            self.preedit_client.cancel(utterance_id)
            if self.preedit_client.restore_pending:
                GLib.idle_add(self._show_restore_failed_status)

    def _handle_cancel(self) -> None:
        """Cancel both ASR and any focus-bound inline preedit."""
        self._cancel_preedit_route()
        self._paste_target = None
        if self.transcription_manager:
            self.transcription_manager.handle_cancel()

    def _on_cancel_enabled_changed(self, enabled: bool) -> None:
        if self.hotkey_manager:
            self.hotkey_manager.set_cancel_enabled(enabled)

    def _on_recording_state_changed(self, app_state, state_str: str) -> None:
        if self.ptt_button:
            self.ptt_button.set_state(state_str)
        if state_str == RecordingState.IDLE.value:
            self._cancel_preedit_route()
            self._paste_target = None

    def _on_error_message_changed(self, app_state, message: str) -> None:
        if message:
            self._cancel_preedit_route()
            self._paste_target = None
        if not self.ptt_button:
            return
        if message:
            self.ptt_button.set_error(message)
        else:
            self.ptt_button.clear_error()

    def _on_login_status_changed(self, app_state, status_str: str) -> None:
        if self.ptt_button:
            if status_str != LoginStatus.LOGGED_IN.value:
                self.ptt_button.hide()
            else:
                self.ptt_button.show()

    def _on_auth_expired(self) -> None:
        """Show re-login dialog."""
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text="认证已过期",
        )
        dialog.set_property("secondary-text", "豆包登录凭证已失效，是否重新登录？")
        dialog.connect("response", self._on_relogin_response)
        dialog.present()

    def _on_relogin_response(self, dialog, response) -> None:
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            self._show_login()

    def _do_logout(self) -> None:
        if not backend_needs_doubao_login():
            logger.info("Ignoring doubao logout for an external backend")
            return
        ParamsStore.clear()
        self.app_state.login_status = LoginStatus.NOT_LOGGED_IN
        if self.login_window:
            self.login_window.logout()
        elif LoginWindow.is_available():
            self.login_window = LoginWindow(self.app_state)
            self.login_window._on_login_status_change = self._on_login_detected
            self.login_window.load()
            self.login_window.logout()

    def _show_help(self) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="使用帮助",
        )
        dialog.set_property(
            "secondary-text",
            "1. 点击 🎤 按钮开始录音\n"
            "2. 实时识别草稿会直接显示在当前光标处\n"
            "3. ⏹ 表示正在录音；再次点击即可停止\n"
            "4. ✨ 表示正在进行二遍识别与文本规整\n"
            "5. 最终结果会在原位置提交，并自动切回原输入法\n\n"
            "如果期间切换输入框，本次内容会取消，避免误输入。\n"
            "实时引擎不可用时才会退回安全的最终粘贴方式。\n\n"
            "按 ESC 键可取消当前录音\n\n"
            "快捷键：\n"
            "  右 Alt 键：切换录音\n"
            "  ESC 键：取消录音\n"
            "  Ctrl + Super + Shift：显示 / 隐藏软键盘",
        )
        dialog.connect("response", lambda d, _: d.destroy())
        dialog.present()

    def _quit(self) -> None:
        """Clean shutdown."""
        self._handle_cancel()
        self.preedit_client.close()
        if self.hotkey_manager:
            self.hotkey_manager.stop()
        self.release()
        self.quit()
