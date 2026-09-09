"""Controller-only GTK application for the standalone voice daemon."""

# gi.require_version() must precede gi.repository imports.
# ruff: noqa: E402

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from doubao_murmur.app_state import (
    AppState,
    LoginStatus,
    RecordingState,
    StatusNotice,
)
from doubao_murmur.controller_worker import ControllerWorker
from doubao_murmur.daemon_control import DaemonController, DaemonReply
from doubao_murmur.hotkey.evdev_listener import EvdevListener
from doubao_murmur.hotkey.manager import HotkeyManager
from doubao_murmur.hotkey.overlay_button import OverlayButton
from doubao_murmur.hotkey.x11_listener import X11KeyListener
from doubao_murmur.ui.tray_icon import TrayIcon

logger = logging.getLogger(__name__)

STATUS_INTERVAL_MS = 500
_ACTIVE_STATES = frozenset({"starting", "recording", "stopping", "observing"})
_STATE_MAP = {
    "idle": RecordingState.IDLE,
    "starting": RecordingState.STARTING,
    "recording": RecordingState.RECORDING,
    "stopping": RecordingState.STOPPING,
    "observing": RecordingState.OBSERVING,
}
_CONTROL_ERRORS = {
    "runtime-unavailable": "语音服务运行目录不可用",
    "socket-unavailable": "独立语音服务不可用，请先启动服务",
    "request-timeout": "独立语音服务响应超时",
    "invalid-response": "独立语音服务返回了无效状态",
    "response-too-large": "独立语音服务返回状态过大",
}
_DAEMON_ERRORS = {
    "daemon-closed": "独立语音服务已关闭",
    "session-active": "已有语音任务正在处理",
    "preedit-unavailable": "光标内语音输入服务不可用",
    "preedit-rejected": "当前输入框不允许语音输入",
    "start-timeout": "语音输入启动超时",
    "preedit-lost": "输入焦点已改变，本次语音已取消",
    "recognition-context-invalid": "语音词表或纠错配置无效",
    "clipboard-unavailable": (
        "本地图形会话或剪贴板工具不可用；请确认 DISPLAY／WAYLAND_DISPLAY 环境，"
        "并安装 xclip（X11）或 wl-clipboard（Wayland）"
    ),
    "clipboard-copy-failed": "终稿未能安全复制；没有自动粘贴或改写远端输入框",
    "microphone-unavailable": "没有可用的麦克风",
    "capture-start-failed": "麦克风启动失败",
    "provider-error": "语音识别服务发生错误",
    "provider-auth": "语音识别服务认证失败",
    "preedit-final-rejected": "最终结果未能提交到原输入框",
    "final-timeout": "等待最终识别结果超时",
    "adaptive-correction-failed": "本次自动纠错学习未能保存",
    "audio-backpressure": "网络发送持续阻塞，本次语音已安全取消",
    "recording-limit-warning": "本次录音将在一分钟内达到时长上限",
}
_STATUS_NOTICES = {
    "clipboard-armed": StatusNotice.CLIPBOARD_ARMED,
    "clipboard-ready": StatusNotice.CLIPBOARD_READY,
}


class DoubaoMurmurApp(Gtk.Application):
    """Right-Alt, floating-button and tray controller; no ASR implementation."""

    def __init__(self, controller: DaemonController | None = None) -> None:
        super().__init__(
            application_id="com.doubao.Murmur",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.app_state = AppState()
        self.controller = controller or DaemonController()
        self.worker: ControllerWorker | None = None
        self.tray_icon: TrayIcon | None = None
        self.hotkey_manager: HotkeyManager | None = None
        self.ptt_button: OverlayButton | None = None
        self._remote_state = "idle"
        self._status_source_id: int | None = None
        self._last_completion_sequence = 0
        self._latest_user_sequence = 0
        self._pending_stop_ui = False
        self._setup_done = False
        self._quit_requested = False

    def do_activate(self) -> None:
        if self._setup_done:
            if self._quit_requested:
                return
            if self.ptt_button:
                self.ptt_button.show()
            if self.tray_icon:
                self.tray_icon.show_window()
            if self.worker:
                self.worker.submit_status()
            return
        self._setup_done = True
        self.hold()
        self._setup_components()

    def _setup_components(self) -> None:
        # Login and provider configuration belong to the standalone daemon.
        # Reuse the historical logged-in value only to keep generic UI state
        # compatible with older widgets; this process reads no credential.
        self.app_state.login_status = LoginStatus.LOGGED_IN

        self.ptt_button = OverlayButton(
            on_press=self._handle_toggle,
            on_cancel=self._handle_cancel,
        )
        self.ptt_button.create()

        self.hotkey_manager = HotkeyManager()
        self.hotkey_manager.on_toggle = self._handle_toggle
        self.hotkey_manager.on_cancel = self._handle_cancel

        x11 = None
        evdev = None
        if X11KeyListener.is_available():
            x11 = X11KeyListener(
                on_toggle=self.hotkey_manager.trigger_toggle,
                on_escape=self.hotkey_manager.trigger_cancel,
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
        # Cached daemon state can be stale while a long start request is in
        # flight. ESC must therefore always reach the worker, which safely
        # turns an idle cancel into a no-op response.
        self.hotkey_manager.set_cancel_enabled(True)

        self.app_state.connect(
            "recording-state-changed", self._on_recording_state_changed
        )
        self.app_state.connect("error-message-changed", self._on_error_message_changed)
        self.app_state.connect("status-notice-changed", self._on_status_notice_changed)
        self.ptt_button.show()

        self.tray_icon = TrayIcon(
            app_state=self.app_state,
            on_quit_clicked=self._quit,
            on_help_clicked=self._show_help,
        )
        self.tray_icon.start()

        self.worker = ControllerWorker(
            self.controller,
            completion=self._on_command_complete,
            post=GLib.idle_add,
        )
        # Startup is observational only. It must never start recording.
        self.worker.submit_status()
        logger.info("Controller-only compatibility UI initialized")

    def _handle_toggle(self) -> None:
        worker = self.worker
        if worker is None or self._quit_requested:
            return
        state = self.app_state.recording_state
        if state is RecordingState.STOPPING:
            return
        if state is RecordingState.IDLE:
            intent = "start"
            optimistic = RecordingState.STARTING
        elif state is RecordingState.OBSERVING:
            # The daemon's toggle is the only atomic operation that finishes
            # the observation lease and starts the next utterance.
            intent = "restart"
            optimistic = RecordingState.STARTING
        else:
            intent = "stop"
            optimistic = RecordingState.STOPPING
        sequence = worker.submit_toggle(intent)
        if sequence is not None:
            self._latest_user_sequence = max(self._latest_user_sequence, sequence)
            if state is RecordingState.STARTING and intent == "stop":
                self._pending_stop_ui = True
            self.app_state.error_message = None
            self.app_state.recording_state = optimistic
            if self.hotkey_manager:
                self.hotkey_manager.set_cancel_enabled(True)

    def _handle_cancel(self) -> None:
        worker = self.worker
        if worker is None or self._quit_requested:
            return
        # Cancel is always queued, even when cached status is idle. This makes
        # it safely follow an uncertain or still-running start request.
        sequence = worker.submit_cancel()
        if sequence is not None:
            self._latest_user_sequence = max(self._latest_user_sequence, sequence)
            self._pending_stop_ui = False
            self.app_state.error_message = None
            if self.app_state.recording_state is not RecordingState.IDLE:
                self.app_state.recording_state = RecordingState.STOPPING

    def _on_command_complete(
        self,
        sequence: int,
        command: str,
        reply: DaemonReply | None,
        error_code: str | None,
    ) -> bool:
        if sequence <= self._last_completion_sequence:
            return GLib.SOURCE_REMOVE
        self._last_completion_sequence = sequence
        if sequence < self._latest_user_sequence:
            return GLib.SOURCE_REMOVE

        if error_code is not None:
            logger.warning("Daemon control request failed (%s)", error_code)
            self.app_state.error_message = _CONTROL_ERRORS.get(
                error_code, "独立语音服务控制失败"
            )
            self._stop_status_monitor()
        elif reply is not None:
            self._apply_reply(command, reply)

        if self._quit_requested and command == "cancel":
            self._finish_quit()
        return GLib.SOURCE_REMOVE

    def _apply_reply(self, command: str, reply: DaemonReply) -> None:
        self._remote_state = reply.state
        display_state = _STATE_MAP[reply.state]
        if command in {"start", "toggle"} and self._pending_stop_ui:
            if reply.ok and reply.state in {"starting", "recording", "observing"}:
                display_state = RecordingState.STOPPING
            else:
                self._pending_stop_ui = False
        elif command in {"stop", "cancel"}:
            self._pending_stop_ui = False
        elif command in {"start", "toggle"} and reply.state in {"idle", "stopping"}:
            self._pending_stop_ui = False
        self.app_state.recording_state = display_state

        notice = _STATUS_NOTICES.get(reply.code)
        if notice is not None:
            self.app_state.status_notice = notice
        elif command == "status" and reply.state == "idle":
            # The daemon reports clipboard-armed only while idle. Active
            # status polls must therefore retain the last content-free mode;
            # a fresh ordinary idle status is what supersedes it.
            self.app_state.status_notice = StatusNotice.NONE

        message = _DAEMON_ERRORS.get(reply.code)
        if message is not None:
            self.app_state.error_message = message
        elif not reply.ok and not (
            command == "cancel" and reply.code == "no-active-session"
        ):
            self.app_state.error_message = "独立语音服务拒绝了本次操作"
        else:
            # A successful reply must clear a previously sticky warning icon.
            self.app_state.error_message = None

        if reply.state in _ACTIVE_STATES:
            self._ensure_status_monitor()
        else:
            self._stop_status_monitor()

    def _ensure_status_monitor(self) -> None:
        if self._status_source_id is None and not self._quit_requested:
            self._status_source_id = GLib.timeout_add(
                STATUS_INTERVAL_MS, self._poll_status
            )

    def _stop_status_monitor(self) -> None:
        source_id = self._status_source_id
        self._status_source_id = None
        if source_id is not None:
            try:
                GLib.source_remove(source_id)
            except GLib.Error:
                pass

    def _poll_status(self) -> bool:
        if self._quit_requested or self._remote_state == "idle":
            self._status_source_id = None
            return GLib.SOURCE_REMOVE
        if self.worker:
            self.worker.submit_status()
        return GLib.SOURCE_CONTINUE

    def _on_recording_state_changed(self, _app_state, state_str: str) -> None:
        if self.ptt_button:
            self.ptt_button.set_state(state_str)

    def _on_error_message_changed(self, _app_state, message: str) -> None:
        if not self.ptt_button:
            return
        if message:
            self.ptt_button.set_error(message)
        else:
            self.ptt_button.clear_error()

    def _on_status_notice_changed(self, _app_state, notice: str) -> None:
        if self.ptt_button:
            self.ptt_button.set_notice(notice)

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
            "右 Alt 或悬浮 🎤：开始／停止语音输入\n"
            "ESC：取消当前语音输入\n\n"
            "录音、识别、光标内提交和自动纠错均由独立的 "
            "Open Voice Input Linux 服务完成。本兼容界面只发送本地控制命令，"
            "不会访问麦克风、供应商网络或 API Key。",
        )
        dialog.connect("response", lambda d, _: d.destroy())
        dialog.present()

    def _quit(self) -> None:
        if self._quit_requested:
            return
        self._quit_requested = True
        self._stop_status_monitor()
        if self.hotkey_manager:
            self.hotkey_manager.stop()
        # Clear unsent toggles and asynchronously cancel any active/uncertain
        # session before allowing the controller process to exit.
        if self.worker:
            sequence = self.worker.submit_cancel()
            if sequence is not None:
                self._latest_user_sequence = max(self._latest_user_sequence, sequence)
                self._pending_stop_ui = False
                self.app_state.recording_state = RecordingState.STOPPING
                return
        self._finish_quit()

    def _finish_quit(self) -> None:
        if self.worker:
            self.worker.close()
            self.worker = None
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.release()
        self.quit()
