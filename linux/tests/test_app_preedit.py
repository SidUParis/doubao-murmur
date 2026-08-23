"""App-boundary tests for strict inline-preedit versus paste routing."""

from __future__ import annotations

from unittest.mock import Mock

from doubao_murmur import app as app_module
from doubao_murmur.app_state import LoginStatus, RecordingState
from doubao_murmur.paste.paste_helper import PasteTarget
from doubao_murmur.preedit_client import AcquireResult


class _Preedit:
    def __init__(self, acquisition: AcquireResult) -> None:
        self.acquisition = acquisition
        self.active = False
        self.restore_pending = False
        self.utterance_id: str | None = None
        self.calls: list[tuple] = []

    def acquire_result(self, utterance_id: str) -> AcquireResult:
        self.calls.append(("acquire", utterance_id))
        if self.acquisition is AcquireResult.ACQUIRED:
            self.active = True
            self.utterance_id = utterance_id
        return self.acquisition

    def partial(self, utterance_id: str, revision: int, text: str) -> bool:
        self.calls.append(("partial", utterance_id, revision, text))
        return self.active and utterance_id == self.utterance_id

    def final(self, utterance_id: str, revision: int, text: str) -> bool:
        self.calls.append(("final", utterance_id, revision, text))
        accepted = self.active and utterance_id == self.utterance_id
        self.active = False
        self.utterance_id = None
        return accepted

    def cancel(self, utterance_id: str) -> bool:
        self.calls.append(("cancel", utterance_id))
        accepted = self.active and utterance_id == self.utterance_id
        self.active = False
        self.utterance_id = None
        return accepted

    def close(self) -> None:
        self.calls.append(("close",))
        self.active = False
        self.utterance_id = None


class _TranscriptionManager:
    def __init__(self, app_state) -> None:
        self.app_state = app_state
        self.toggle_calls = 0
        self.cancel_calls = 0
        self.reload_calls = 0
        self.reload_result = True
        self.backend_name = "volcengine"
        self.on_auth_expired = None
        self.on_show_login = None
        self.on_paste = None
        self.on_params_needed = None
        self.on_cancel_enabled_changed = None
        self.on_overlay_update = None

    def handle_toggle(self) -> None:
        self.toggle_calls += 1
        state = self.app_state.recording_state
        if state == RecordingState.IDLE:
            self.app_state.recording_state = RecordingState.STARTING
        elif state in (RecordingState.STARTING, RecordingState.RECORDING):
            self.app_state.recording_state = RecordingState.STOPPING

    def handle_cancel(self) -> None:
        self.cancel_calls += 1
        self.app_state.recording_state = RecordingState.IDLE

    def reload_backend(self) -> bool:
        self.reload_calls += 1
        return self.reload_result


class _OverlayButton:
    def __init__(self, on_press, on_cancel) -> None:
        self.on_press = on_press
        self.on_cancel = on_cancel
        self.errors: list[str] = []
        self.states: list[str] = []
        self.show_calls = 0
        self.hide_calls = 0

    def create(self) -> None:
        pass

    def show(self) -> None:
        self.show_calls += 1

    def hide(self) -> None:
        self.hide_calls += 1

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def set_error(self, message: str) -> None:
        self.errors.append(message)

    def clear_error(self) -> None:
        pass


class _HotkeyManager:
    def __init__(self) -> None:
        self.on_toggle = None
        self.on_cancel = None
        self.on_keyboard = None

    def start(self, **_kwargs) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_cancel_enabled(self, _enabled: bool) -> None:
        pass

    def trigger_toggle(self) -> None:
        if self.on_toggle:
            self.on_toggle()

    def trigger_cancel(self) -> None:
        if self.on_cancel:
            self.on_cancel()

    def trigger_keyboard(self) -> None:
        if self.on_keyboard:
            self.on_keyboard()


class _UnavailableListener:
    @staticmethod
    def is_available() -> bool:
        return False


class _Keyboard:
    pass


class _TrayIcon:
    def __init__(self, **kwargs) -> None:
        self.on_backend_reload = kwargs["on_backend_reload"]
        self.uses_doubao_login = kwargs["uses_doubao_login"]
        self.uses_doubao_updates: list[bool] = []

    def start(self) -> None:
        pass

    def set_uses_doubao_login(self, enabled: bool) -> None:
        self.uses_doubao_login = enabled
        self.uses_doubao_updates.append(enabled)


def _configured_app(monkeypatch, acquisition: AcquireResult):
    preedit = _Preedit(acquisition)
    capture_target = Mock(return_value=PasteTarget("window-42"))
    copy_and_paste = Mock(return_value=True)

    monkeypatch.setattr(app_module, "PreeditClient", lambda: preedit)
    monkeypatch.setattr(app_module, "TranscriptionManager", _TranscriptionManager)
    monkeypatch.setattr(app_module, "OverlayButton", _OverlayButton)
    monkeypatch.setattr(app_module, "HotkeyManager", _HotkeyManager)
    monkeypatch.setattr(app_module, "X11KeyListener", _UnavailableListener)
    monkeypatch.setattr(app_module, "EvdevListener", _UnavailableListener)
    monkeypatch.setattr(app_module, "KeyboardWindow", _Keyboard)
    monkeypatch.setattr(app_module, "TrayIcon", _TrayIcon)
    monkeypatch.setattr(app_module, "backend_needs_doubao_login", lambda: False)
    monkeypatch.setattr(app_module.PasteHelper, "capture_target", capture_target)
    monkeypatch.setattr(app_module.PasteHelper, "copy_and_paste", copy_and_paste)

    app = app_module.DoubaoMurmurApp()
    app._setup_components()
    manager = app.transcription_manager
    assert isinstance(manager, _TranscriptionManager)
    return app, manager, preedit, capture_target, copy_and_paste


def test_acquired_session_routes_partial_and_final_without_paste(monkeypatch):
    app, manager, preedit, capture, paste = _configured_app(
        monkeypatch, AcquireResult.ACQUIRED
    )

    app._handle_toggle()
    assert manager.toggle_calls == 1
    assert app.app_state.recording_state == RecordingState.STARTING
    assert not capture.called

    manager.on_overlay_update("实时草稿")
    manager.on_paste("二遍最终结果。")

    utterance_id = preedit.calls[0][1]
    assert preedit.calls == [
        ("acquire", utterance_id),
        ("partial", utterance_id, 1, "实时草稿"),
        ("final", utterance_id, 2, "二遍最终结果。"),
    ]
    assert not paste.called


def test_saved_key_callback_reloads_backend_and_refreshes_login_ui(monkeypatch):
    app, manager, _preedit, _capture, _paste = _configured_app(
        monkeypatch, AcquireResult.UNAVAILABLE
    )
    app.app_state.login_status = LoginStatus.NOT_LOGGED_IN
    show_calls_before = app.ptt_button.show_calls

    assert app.tray_icon.on_backend_reload() is True

    assert manager.reload_calls == 1
    assert app.tray_icon.uses_doubao_updates == [False]
    assert app.app_state.login_status == LoginStatus.LOGGED_IN
    assert app.ptt_button.show_calls > show_calls_before


def test_busy_backend_reload_does_not_change_login_ui(monkeypatch):
    app, manager, _preedit, _capture, _paste = _configured_app(
        monkeypatch, AcquireResult.UNAVAILABLE
    )
    manager.reload_result = False
    app.app_state.login_status = LoginStatus.NOT_LOGGED_IN

    assert app.tray_icon.on_backend_reload() is False

    assert manager.reload_calls == 1
    assert app.tray_icon.uses_doubao_updates == []
    assert app.app_state.login_status == LoginStatus.NOT_LOGGED_IN


def test_failed_partial_after_acquire_cancels_without_paste_fallback(monkeypatch):
    app, manager, preedit, _capture, paste = _configured_app(
        monkeypatch, AcquireResult.ACQUIRED
    )
    app._handle_toggle()
    utterance_id = preedit.calls[0][1]
    preedit.partial = Mock(return_value=False)

    manager.on_overlay_update("不能转成粘贴的草稿")

    preedit.partial.assert_called_once_with(utterance_id, 1, "不能转成粘贴的草稿")
    assert ("cancel", utterance_id) in preedit.calls
    assert manager.cancel_calls == 1
    assert not paste.called


def test_failed_final_after_acquire_never_uses_any_paste_helper(monkeypatch):
    app, manager, preedit, _capture, paste = _configured_app(
        monkeypatch, AcquireResult.ACQUIRED
    )
    copy_only = Mock()
    monkeypatch.setattr(app_module.PasteHelper, "copy_only", copy_only)
    app._handle_toggle()
    utterance_id = preedit.calls[0][1]
    preedit.final = Mock(return_value=False)

    manager.on_paste("不能转进剪贴板的最终文本")

    preedit.final.assert_called_once_with(utterance_id, 1, "不能转进剪贴板的最终文本")
    assert not paste.called
    assert not copy_only.called


def test_rejected_acquire_does_not_start_microphone_or_paste(monkeypatch):
    app, manager, preedit, capture, paste = _configured_app(
        monkeypatch, AcquireResult.REJECTED
    )

    app._handle_toggle()

    assert [call[0] for call in preedit.calls] == ["acquire"]
    # handle_toggle is the only app gateway to TranscriptionManager._start_recording.
    assert manager.toggle_calls == 0
    assert app.app_state.recording_state == RecordingState.IDLE
    assert not capture.called
    assert not paste.called


def test_unavailable_preedit_uses_existing_paste_fallback(monkeypatch):
    app, manager, preedit, capture, paste = _configured_app(
        monkeypatch, AcquireResult.UNAVAILABLE
    )

    app._handle_toggle()
    assert manager.toggle_calls == 1
    capture.assert_called_once_with()

    manager.on_overlay_update("实时草稿")
    manager.on_paste("最终结果。")

    assert [call[0] for call in preedit.calls] == ["acquire"]
    paste.assert_called_once_with("最终结果。", target=PasteTarget("window-42"))


def test_cancel_clears_acquired_preedit_and_ignores_late_final(monkeypatch):
    app, manager, preedit, _capture, paste = _configured_app(
        monkeypatch, AcquireResult.ACQUIRED
    )
    app._handle_toggle()
    utterance_id = preedit.calls[0][1]

    app.ptt_button.on_cancel()

    assert ("cancel", utterance_id) in preedit.calls
    assert manager.cancel_calls == 1
    assert app.app_state.recording_state == RecordingState.IDLE
    manager.on_paste("取消后迟到的最终结果")
    assert not paste.called
    assert not any(call[0] == "final" for call in preedit.calls)


def test_error_clears_acquired_preedit_and_ignores_late_final(monkeypatch):
    app, manager, preedit, _capture, paste = _configured_app(
        monkeypatch, AcquireResult.ACQUIRED
    )
    app._handle_toggle()
    utterance_id = preedit.calls[0][1]

    app.app_state.error_message = "连接出错"

    assert ("cancel", utterance_id) in preedit.calls
    manager.on_paste("错误后迟到的最终结果")
    assert not paste.called
    assert not any(call[0] == "final" for call in preedit.calls)
