"""GTK boundary tests for the controller-only compatibility application."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from doubao_murmur import app as app_module
from doubao_murmur.app_state import RecordingState, StatusNotice
from doubao_murmur.daemon_control import DaemonReply


class _Worker:
    instances = []

    def __init__(self, controller, *, completion, post) -> None:
        self.controller = controller
        self.completion = completion
        self.post = post
        self.status_calls = 0
        self.toggle_intents: list[str] = []
        self.cancel_calls = 0
        self.closed = False
        self.sequence = 0
        self.__class__.instances.append(self)

    def submit_status(self) -> int:
        self.status_calls += 1
        self.sequence += 1
        return self.sequence

    def submit_toggle(self, intent: str) -> int:
        self.toggle_intents.append(intent)
        self.sequence += 1
        return self.sequence

    def submit_cancel(self) -> int:
        self.cancel_calls += 1
        self.sequence += 1
        return self.sequence

    def close(self) -> None:
        self.closed = True


class _OverlayButton:
    def __init__(self, on_press, on_cancel) -> None:
        self.on_press = on_press
        self.on_cancel = on_cancel
        self.states: list[str] = []
        self.errors: list[str] = []
        self.notices: list[str] = []
        self.clear_calls = 0
        self.show_calls = 0

    def create(self) -> None:
        pass

    def show(self) -> None:
        self.show_calls += 1

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def set_error(self, message: str) -> None:
        self.errors.append(message)

    def clear_error(self) -> None:
        self.clear_calls += 1

    def set_notice(self, notice: str) -> None:
        self.notices.append(notice)


class _HotkeyManager:
    def __init__(self) -> None:
        self.on_toggle = None
        self.on_cancel = None
        self.cancel_enabled: list[bool] = []
        self.stopped = False

    def start(self, **_kwargs) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True

    def set_cancel_enabled(self, enabled: bool) -> None:
        self.cancel_enabled.append(enabled)

    def trigger_toggle(self) -> None:
        if self.on_toggle:
            self.on_toggle()

    def trigger_cancel(self) -> None:
        if self.on_cancel:
            self.on_cancel()


class _UnavailableListener:
    @staticmethod
    def is_available() -> bool:
        return False


class _TrayIcon:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.show_calls = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def show_window(self) -> None:
        self.show_calls += 1


def _configured_app(monkeypatch):
    _Worker.instances.clear()
    monkeypatch.setattr(app_module, "ControllerWorker", _Worker)
    monkeypatch.setattr(app_module, "OverlayButton", _OverlayButton)
    monkeypatch.setattr(app_module, "HotkeyManager", _HotkeyManager)
    monkeypatch.setattr(app_module, "X11KeyListener", _UnavailableListener)
    monkeypatch.setattr(app_module, "EvdevListener", _UnavailableListener)
    monkeypatch.setattr(app_module, "TrayIcon", _TrayIcon)
    app = app_module.DoubaoMurmurApp(controller=object())
    app._setup_components()
    return app, _Worker.instances[-1]


def test_startup_only_requests_status(monkeypatch):
    app, worker = _configured_app(monkeypatch)

    assert worker.status_calls == 1
    assert worker.toggle_intents == []
    assert worker.cancel_calls == 0
    assert app.ptt_button.show_calls == 1


def test_right_alt_and_floating_button_share_toggle_path(monkeypatch):
    app, worker = _configured_app(monkeypatch)

    app.hotkey_manager.trigger_toggle()
    assert worker.toggle_intents == ["start"]
    assert app.app_state.recording_state is RecordingState.STARTING

    app.hotkey_manager.trigger_toggle()
    assert worker.toggle_intents == ["start", "stop"]
    assert app.app_state.recording_state is RecordingState.STOPPING

    app.app_state.recording_state = RecordingState.IDLE
    app.ptt_button.on_press()
    assert worker.toggle_intents == ["start", "stop", "start"]


def test_startup_status_cannot_undo_newer_optimistic_toggle(monkeypatch):
    app, _worker = _configured_app(monkeypatch)
    app._handle_toggle()
    assert app.app_state.recording_state is RecordingState.STARTING

    # Sequence 1 was the startup status; the user toggle is sequence 2.
    app._on_command_complete(
        1,
        "status",
        DaemonReply(True, "status", "idle"),
        None,
    )

    assert app.app_state.recording_state is RecordingState.STARTING


@pytest.mark.parametrize("command", ["start", "toggle"])
def test_start_reply_does_not_undo_a_pending_stop_indicator(monkeypatch, command):
    app, _worker = _configured_app(monkeypatch)
    app._pending_stop_ui = True
    app.app_state.recording_state = RecordingState.STOPPING

    app._on_command_complete(
        2,
        command,
        DaemonReply(True, "started", "starting"),
        None,
    )

    assert app.app_state.recording_state is RecordingState.STOPPING


def test_failed_explicit_start_clears_pending_stop_indicator(monkeypatch):
    app, _worker = _configured_app(monkeypatch)
    app._pending_stop_ui = True
    app.app_state.recording_state = RecordingState.STOPPING

    app._on_command_complete(
        2,
        "start",
        DaemonReply(False, "preedit-rejected", "idle"),
        None,
    )

    assert app.app_state.recording_state is RecordingState.IDLE
    assert app._pending_stop_ui is False


def test_rejected_start_does_not_fake_a_contingent_stop(monkeypatch):
    app, _worker = _configured_app(monkeypatch)
    app._pending_stop_ui = True
    app.app_state.recording_state = RecordingState.STOPPING

    app._on_command_complete(
        2,
        "start",
        DaemonReply(False, "session-active", "recording"),
        None,
    )

    assert app.app_state.recording_state is RecordingState.RECORDING
    assert app._pending_stop_ui is False


def test_escape_always_queues_cancel_even_when_cached_idle(monkeypatch):
    app, worker = _configured_app(monkeypatch)
    assert app.app_state.recording_state is RecordingState.IDLE

    app.hotkey_manager.trigger_cancel()

    assert worker.cancel_calls == 1
    assert app.hotkey_manager.cancel_enabled[-1] is True


def test_observing_is_explicit_and_next_toggle_starts(monkeypatch):
    app, worker = _configured_app(monkeypatch)
    timeout_add = Mock(return_value=77)
    monkeypatch.setattr(app_module.GLib, "timeout_add", timeout_add)

    app._on_command_complete(
        1,
        "status",
        DaemonReply(True, "status", "observing"),
        None,
    )

    assert app.app_state.recording_state is RecordingState.OBSERVING
    assert app.ptt_button.states[-1] == "observing"
    assert timeout_add.call_args.args[0] == app_module.STATUS_INTERVAL_MS

    app._handle_toggle()
    assert worker.toggle_intents == ["restart"]
    assert app.app_state.recording_state is RecordingState.STARTING


def test_successful_idle_reply_clears_sticky_error(monkeypatch):
    app, _worker = _configured_app(monkeypatch)
    app.app_state.error_message = "旧错误"
    assert app.ptt_button.errors[-1] == "旧错误"

    app._on_command_complete(
        1,
        "status",
        DaemonReply(True, "status", "idle"),
        None,
    )

    assert app.app_state.error_message is None
    assert app.ptt_button.clear_calls >= 1


@pytest.mark.parametrize(
    ("code", "notice"),
    [
        ("clipboard-armed", StatusNotice.CLIPBOARD_ARMED),
        ("clipboard-ready", StatusNotice.CLIPBOARD_READY),
    ],
)
def test_clipboard_status_codes_are_content_free_notices_not_errors(
    monkeypatch, code, notice
):
    app, _worker = _configured_app(monkeypatch)

    app._on_command_complete(
        1,
        "status",
        DaemonReply(True, code, "idle"),
        None,
    )

    assert app.app_state.status_notice is notice
    assert app.app_state.error_message is None
    assert app.ptt_button.notices[-1] == notice.value
    assert app.ptt_button.errors == []


def test_plain_idle_status_clears_a_stale_clipboard_notice(monkeypatch):
    app, _worker = _configured_app(monkeypatch)
    app.app_state.status_notice = StatusNotice.CLIPBOARD_ARMED

    app._on_command_complete(
        1,
        "status",
        DaemonReply(True, "status", "idle"),
        None,
    )

    assert app.app_state.status_notice is StatusNotice.NONE
    assert app.ptt_button.notices[-1] == ""


def test_non_status_idle_reply_does_not_erase_persistent_clipboard_mode(monkeypatch):
    app, _worker = _configured_app(monkeypatch)
    app.app_state.status_notice = StatusNotice.CLIPBOARD_ARMED

    app._on_command_complete(
        1,
        "cancel",
        DaemonReply(False, "no-active-session", "idle"),
        None,
    )

    assert app.app_state.status_notice is StatusNotice.CLIPBOARD_ARMED


def test_late_completion_cannot_overwrite_newer_state(monkeypatch):
    app, _worker = _configured_app(monkeypatch)
    app._on_command_complete(
        4,
        "status",
        DaemonReply(True, "status", "recording"),
        None,
    )

    app._on_command_complete(
        3,
        "status",
        DaemonReply(True, "status", "idle"),
        None,
    )

    assert app.app_state.recording_state is RecordingState.RECORDING


def test_control_error_is_bounded_and_does_not_expose_exception(monkeypatch):
    app, _worker = _configured_app(monkeypatch)

    app._on_command_complete(1, "status", None, "socket-unavailable")

    assert app.app_state.error_message == "独立语音服务不可用，请先启动服务"
    assert app.ptt_button.errors[-1] == app.app_state.error_message


@pytest.mark.parametrize(
    ("code", "state", "message"),
    [
        (
            "audio-backpressure",
            "idle",
            "网络发送持续阻塞，本次语音已安全取消",
        ),
        (
            "recording-limit-warning",
            "recording",
            "本次录音将在一分钟内达到时长上限",
        ),
    ],
)
def test_daemon_warning_and_audio_failure_are_visible(
    monkeypatch, code, state, message
):
    app, _worker = _configured_app(monkeypatch)

    app._on_command_complete(1, "status", DaemonReply(True, code, state), None)

    assert app.app_state.error_message == message
    assert app.ptt_button.errors[-1] == message


def test_quit_cancels_asynchronously_before_closing(monkeypatch):
    app, worker = _configured_app(monkeypatch)
    finish = Mock()
    monkeypatch.setattr(app, "_finish_quit", finish)

    app._quit()

    assert worker.cancel_calls == 1
    assert app.hotkey_manager.stopped
    assert not finish.called

    app._on_command_complete(
        2,
        "cancel",
        DaemonReply(True, "cancelled", "idle"),
        None,
    )
    finish.assert_called_once_with()


def test_app_module_has_no_legacy_voice_or_paste_gateway():
    for name in (
        "TranscriptionManager",
        "AudioCaptureManager",
        "PreeditClient",
        "PasteHelper",
        "DoubaoASRClient",
    ):
        assert not hasattr(app_module, name)
