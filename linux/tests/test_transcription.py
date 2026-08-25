"""Backend-selection and lifecycle tests for TranscriptionManager."""

import logging
import sys
from types import ModuleType, SimpleNamespace

import pytest

from doubao_murmur.app_state import LoginStatus, RecordingState
from doubao_murmur.audio_capture import AudioDeviceError
from doubao_murmur.config import STOP_SAFETY_TIMEOUT
from doubao_murmur import transcription


class _Audio:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.on_audio_data = None

    def start(self, on_audio_data):
        self.started += 1
        self.on_audio_data = on_audio_data

    def stop(self):
        self.stopped += 1


class _Client:
    def __init__(
        self,
        final_result_timeout=None,
        *,
        is_streaming=True,
        waits_for_final_event=False,
    ):
        if final_result_timeout is not None:
            self.final_result_timeout = final_result_timeout
        self.is_streaming = is_streaming
        self.waits_for_final_event = waits_for_final_event
        self.finished = 0
        self.disconnected = 0
        self.connected = []

    def connect(self, params):
        self.connected.append(params)

    def send_audio(self, data):
        del data

    def finish_sending(self):
        self.finished += 1

    def disconnect(self):
        self.disconnected += 1


def _bare_manager(client=None):
    manager = transcription.TranscriptionManager.__new__(
        transcription.TranscriptionManager
    )
    manager.app_state = SimpleNamespace(
        recording_state=RecordingState.RECORDING,
        transcription_text="partial",
        error_message=None,
        login_status=LoginStatus.LOGGED_IN,
    )
    manager.asr_client = client or _Client()
    manager.backend_name = "volcengine"
    manager.audio_capture = _Audio()
    manager.using_cached_params = False
    manager.awaiting_final_result = False
    manager.post_stop_frames = 0
    manager.last_post_stop_text = ""
    manager.safety_timer_id = None
    manager.recording_limit_timer_id = None
    manager.max_recording_seconds = 600.0
    manager._session_generation = 0
    manager.on_auth_expired = None
    manager.on_cancel_enabled_changed = None
    manager.on_show_login = None
    manager.on_params_needed = None
    manager.on_overlay_show = None
    manager.on_overlay_hide = None
    manager.on_overlay_update = None
    manager.on_paste = None
    return manager


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 600.0),
        (0.25, 1.0),
        (90, 90.0),
        (7200, 3600.0),
        ("invalid", 600.0),
        (float("inf"), 600.0),
    ],
)
def test_recording_duration_limit_is_bounded(configured, expected):
    assert transcription._bounded_recording_seconds(configured) == expected


def test_recording_limit_timer_is_bound_to_current_session(monkeypatch):
    manager = _bare_manager()
    manager._session_generation = 6
    manager.max_recording_seconds = 600.0
    scheduled = []
    monkeypatch.setattr(
        transcription.GLib,
        "timeout_add",
        lambda delay, callback, generation: scheduled.append(
            (delay, callback, generation)
        )
        or 80,
    )

    manager._arm_recording_limit_timer(6)

    assert scheduled == [(600_000, manager._recording_limit_timeout, 6)]
    assert manager.recording_limit_timer_id == 80


def test_successful_recording_start_arms_duration_limit(monkeypatch):
    manager = _bare_manager()
    manager.app_state.recording_state = RecordingState.IDLE
    scheduled = []
    monkeypatch.setattr(manager, "_harvest_clipboard", lambda: None)
    monkeypatch.setattr(
        transcription.GLib,
        "timeout_add",
        lambda delay, callback, generation: scheduled.append(
            (delay, callback, generation)
        )
        or 81,
    )

    manager._start_recording()

    assert manager._session_generation == 1
    assert manager.audio_capture.started == 1
    assert manager.asr_client.connected == [None]
    assert scheduled == [(600_000, manager._recording_limit_timeout, 1)]
    assert manager.recording_limit_timer_id == 81


def test_microphone_selection_failure_is_shown_without_connecting(monkeypatch):
    manager = _bare_manager()
    manager.app_state.recording_state = RecordingState.IDLE
    scheduled = []
    monkeypatch.setattr(manager, "_harvest_clipboard", lambda: None)
    monkeypatch.setattr(
        manager.audio_capture,
        "start",
        lambda on_audio_data: (_ for _ in ()).throw(
            AudioDeviceError("检测到多个物理麦克风，无法安全自动选择")
        ),
    )
    monkeypatch.setattr(
        transcription.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 82,
    )

    manager._start_recording()

    assert manager.asr_client.connected == []
    assert manager.app_state.error_message == (
        "麦克风不可用：检测到多个物理麦克风，无法安全自动选择"
    )
    assert len(scheduled) == 1


def test_completion_pastes_final_text_without_overlay():
    manager = _bare_manager()
    manager.app_state.transcription_text = "  二遍最终结果。  "
    pasted = []
    resets = []
    manager.on_paste = pasted.append
    manager._reset_to_idle = lambda: resets.append(True)

    manager._complete_transcription()

    assert pasted == ["二遍最终结果。"]
    assert resets == [True]


def test_unknown_backend_uses_doubao_login(monkeypatch):
    monkeypatch.setattr(
        transcription,
        "load_backend_config",
        lambda: {"backend": "typo"},
    )

    assert transcription.configured_backend_name() == "doubao"
    assert transcription.backend_needs_doubao_login()
    assert isinstance(transcription._build_asr_client(), transcription.ASRClient)


def test_build_volcengine_client_with_merged_settings(monkeypatch):
    module = ModuleType("doubao_murmur.volcengine_client")

    class FakeVolcengineASRClient:
        def __init__(self, settings):
            self.settings = settings

    module.VolcengineASRClient = FakeVolcengineASRClient
    monkeypatch.setitem(sys.modules, "doubao_murmur.volcengine_client", module)
    backend = {"backend": "volcengine", "language": "zh-CN"}
    merged = {"api_key": "secret", "language": "zh-CN"}
    monkeypatch.setattr(transcription, "load_backend_config", lambda: backend)
    monkeypatch.setattr(
        transcription,
        "load_volcengine_config",
        lambda settings: merged if settings is backend else {},
    )

    client = transcription._build_asr_client()

    assert isinstance(client, FakeVolcengineASRClient)
    assert client.settings == merged


def test_stop_uses_client_final_result_timeout(monkeypatch):
    manager = _bare_manager(_Client(final_result_timeout=17.5))
    scheduled = []
    monkeypatch.setattr(
        transcription.GLib,
        "timeout_add",
        lambda delay, callback, *args: scheduled.append(
            (delay, callback, args)
        )
        or 91,
    )

    manager._stop_recording()

    assert manager.audio_capture.stopped == 1
    assert manager.asr_client.finished == 1
    assert scheduled == [(17500, manager._safety_timeout, (0,))]
    assert manager.safety_timer_id == 91


def test_stop_rejects_invalid_client_timeout(monkeypatch):
    manager = _bare_manager(_Client(final_result_timeout=-3))
    scheduled = []
    monkeypatch.setattr(
        transcription.GLib,
        "timeout_add",
        lambda delay, callback, *args: scheduled.append(delay) or 92,
    )

    manager._stop_recording()

    assert scheduled == [int(STOP_SAFETY_TIMEOUT * 1000)]


def test_recording_limit_enters_normal_finalization_path(monkeypatch):
    manager = _bare_manager(
        _Client(final_result_timeout=17.5, waits_for_final_event=True)
    )
    manager._session_generation = 7
    manager.recording_limit_timer_id = 81
    scheduled = []
    monkeypatch.setattr(
        transcription.GLib,
        "timeout_add",
        lambda delay, callback, *args: scheduled.append(
            (delay, callback, args)
        )
        or 82,
    )

    result = manager._recording_limit_timeout(7)

    assert result is False
    assert manager.recording_limit_timer_id is None
    assert manager.app_state.recording_state == RecordingState.STOPPING
    assert manager.app_state.error_message is None
    assert manager.audio_capture.stopped == 1
    assert manager.asr_client.finished == 1
    assert manager.awaiting_final_result is True
    assert scheduled == [(17500, manager._safety_timeout, (7,))]
    assert manager.safety_timer_id == 82


def test_stale_recording_limit_cannot_stop_new_session():
    manager = _bare_manager()
    manager._session_generation = 22
    manager.recording_limit_timer_id = 103

    result = manager._recording_limit_timeout(21)

    assert result is False
    assert manager.recording_limit_timer_id == 103
    assert manager.app_state.recording_state == RecordingState.RECORDING
    assert manager.audio_capture.stopped == 0
    assert manager.asr_client.finished == 0


def test_stale_final_safety_timer_cannot_complete_new_session():
    manager = _bare_manager()
    manager._session_generation = 22
    manager.safety_timer_id = 108
    manager.app_state.recording_state = RecordingState.STOPPING
    manager.awaiting_final_result = True
    completions = []
    manager._complete_transcription = lambda: completions.append(True)

    result = manager._safety_timeout(21)

    assert result is False
    assert manager.safety_timer_id == 108
    assert manager.awaiting_final_result is True
    assert completions == []


def test_cancel_clears_timers_and_old_limit_stays_stale(monkeypatch):
    manager = _bare_manager()
    manager._session_generation = 30
    manager.recording_limit_timer_id = 104
    manager.safety_timer_id = 105
    removed = []
    monkeypatch.setattr(transcription.GLib, "source_remove", removed.append)
    monkeypatch.setattr(
        transcription.GLib, "timeout_add", lambda *args: 106
    )

    manager.handle_cancel()

    assert sorted(removed) == [104, 105]
    assert manager.recording_limit_timer_id is None
    assert manager.safety_timer_id is None
    assert manager.app_state.recording_state == RecordingState.IDLE

    # Session 31 was reset/cancelled, then session 32 started with a new timer.
    manager._session_generation = 32
    manager.recording_limit_timer_id = 107
    manager.app_state.recording_state = RecordingState.RECORDING
    stops_before = manager.audio_capture.stopped
    finishes_before = manager.asr_client.finished

    manager._recording_limit_timeout(30)

    assert manager.recording_limit_timer_id == 107
    assert manager.audio_capture.stopped == stops_before
    assert manager.asr_client.finished == finishes_before


def test_asr_error_stops_capture_and_clears_timers_without_logging_text(
    monkeypatch, caplog
):
    manager = _bare_manager()
    manager.recording_limit_timer_id = 108
    manager.safety_timer_id = 109
    manager.awaiting_final_result = True
    removed = []
    scheduled = []
    private_text = "private-dictation-or-key"
    monkeypatch.setattr(transcription.GLib, "source_remove", removed.append)
    monkeypatch.setattr(
        transcription.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 110,
    )
    caplog.set_level(logging.ERROR)

    manager._on_asr_error(RuntimeError(private_text))

    assert sorted(removed) == [108, 109]
    assert manager.recording_limit_timer_id is None
    assert manager.safety_timer_id is None
    assert manager.awaiting_final_result is False
    assert manager.audio_capture.stopped == 1
    assert manager.asr_client.disconnected == 1
    assert manager.app_state.recording_state == RecordingState.STOPPING
    assert manager.app_state.error_message == "连接出错"
    assert len(scheduled) == 1
    assert private_text not in caplog.text


def test_two_pass_client_never_completes_on_live_hypothesis():
    manager = _bare_manager(_Client(is_streaming=True, waits_for_final_event=True))
    manager.app_state.recording_state = RecordingState.STOPPING
    manager.awaiting_final_result = True
    updates = []
    completions = []
    manager.on_overlay_update = updates.append
    manager._complete_transcription = lambda: completions.append(True)

    manager._on_asr_result("嗯那个下周二下午三点")
    manager._on_asr_result("下周二下午3点。")

    assert manager.app_state.transcription_text == "下周二下午3点。"
    assert updates == ["嗯那个下周二下午三点", "下周二下午3点。"]
    assert completions == []


def test_two_pass_client_completes_once_on_explicit_final(monkeypatch):
    manager = _bare_manager(_Client(is_streaming=True, waits_for_final_event=True))
    manager.app_state.recording_state = RecordingState.STOPPING
    manager.awaiting_final_result = True
    manager.safety_timer_id = 77
    completions = []
    removed = []
    manager._complete_transcription = lambda: completions.append(
        manager.app_state.transcription_text
    )
    monkeypatch.setattr(transcription.GLib, "source_remove", removed.append)

    manager._on_asr_result("实时草稿")
    manager._on_asr_result("二遍最终结果。")
    manager._on_asr_finish()
    manager._on_asr_finish()

    assert completions == ["二遍最终结果。"]
    assert removed == [77]
    assert manager.safety_timer_id is None


def test_queued_old_session_callbacks_cannot_enter_new_session(monkeypatch):
    manager = _bare_manager(_Client(is_streaming=True, waits_for_final_event=True))
    manager._session_generation = 11
    manager.app_state.recording_state = RecordingState.STOPPING
    manager.awaiting_final_result = True
    updates = []
    completions = []
    manager.on_overlay_update = updates.append
    manager._complete_transcription = lambda: completions.append(True)
    queued = []

    def queue(callback, *args):
        queued.append((callback, args))
        return len(queued)

    monkeypatch.setattr(transcription.GLib, "idle_add", queue)
    manager._wire_asr_callbacks()

    # These events belong to session A and have already left the worker
    # thread, but GTK has not executed them yet.
    manager.asr_client.on_result("session A final")
    manager.asr_client.on_finish()

    # Cancel A and start B before GTK drains its idle queue.
    manager._session_generation = 13
    manager.app_state.recording_state = RecordingState.RECORDING
    manager.awaiting_final_result = False
    manager.app_state.transcription_text = "session B draft"
    for callback, args in queued:
        callback(*args)

    assert manager.app_state.transcription_text == "session B draft"
    assert updates == []
    assert completions == []


def test_reload_backend_swaps_idle_client_and_reloads_limits(monkeypatch):
    old_client = _Client()
    new_client = _Client()
    manager = _bare_manager(old_client)
    manager.app_state.recording_state = RecordingState.IDLE
    manager.recording_limit_timer_id = 301
    manager.safety_timer_id = 302
    removed = []
    settings = {
        "backend": "volcengine",
        "max_recording_seconds": 45,
    }
    monkeypatch.setattr(transcription, "load_backend_config", lambda: settings)
    monkeypatch.setattr(
        transcription,
        "_build_asr_client",
        lambda received: new_client if received is settings else None,
    )
    monkeypatch.setattr(transcription.GLib, "source_remove", removed.append)

    assert manager.reload_backend() is True

    assert manager.asr_client is new_client
    assert manager.backend_name == "volcengine"
    assert manager.max_recording_seconds == 45.0
    assert manager._session_generation == 1
    assert sorted(removed) == [301, 302]
    assert old_client.disconnected == 1
    assert old_client.on_result is None
    assert callable(new_client.on_result)


def test_reload_backend_refuses_while_recording(monkeypatch):
    old_client = _Client()
    manager = _bare_manager(old_client)

    def unexpected_load():
        raise AssertionError("busy reload must not read new settings")

    monkeypatch.setattr(transcription, "load_backend_config", unexpected_load)

    assert manager.reload_backend() is False
    assert manager.asr_client is old_client
    assert manager._session_generation == 0
    assert old_client.disconnected == 0


def test_reload_backend_build_failure_preserves_client_and_hides_secret(
    monkeypatch, caplog
):
    old_client = _Client()
    manager = _bare_manager(old_client)
    manager.app_state.recording_state = RecordingState.IDLE
    secret = "reload-secret-must-not-be-logged"
    monkeypatch.setattr(
        transcription,
        "load_backend_config",
        lambda: {"backend": "volcengine"},
    )

    def fail_build(_settings):
        raise RuntimeError(secret)

    monkeypatch.setattr(transcription, "_build_asr_client", fail_build)
    caplog.set_level(logging.ERROR)

    assert manager.reload_backend() is False
    assert manager.asr_client is old_client
    assert manager._session_generation == 0
    assert old_client.disconnected == 0
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


def test_reload_backend_rejects_old_client_callbacks(monkeypatch):
    old_client = _Client()
    new_client = _Client()
    manager = _bare_manager(old_client)
    manager.app_state.recording_state = RecordingState.IDLE
    manager._wire_asr_callbacks()
    old_result = old_client.on_result
    updates = []
    queued = []
    manager.on_overlay_update = updates.append

    def queue(callback, *args):
        queued.append((callback, args))
        return len(queued)

    monkeypatch.setattr(transcription.GLib, "idle_add", queue)
    monkeypatch.setattr(
        transcription,
        "load_backend_config",
        lambda: {"backend": "volcengine"},
    )
    monkeypatch.setattr(
        transcription, "_build_asr_client", lambda _settings: new_client
    )

    old_result("queued before reload")
    assert manager.reload_backend() is True
    assert old_result("late after reload") is False
    new_client.on_result("current client result")
    for callback, args in queued:
        callback(*args)

    assert manager.app_state.transcription_text == "current client result"
    assert updates == ["current client result"]


def test_delayed_text_clear_does_not_erase_new_session():
    manager = _bare_manager()
    manager._session_generation = 4
    manager.app_state.transcription_text = "new session draft"

    assert manager._clear_transcription_if_current(3) is False
    assert manager.app_state.transcription_text == "new session draft"

    assert manager._clear_transcription_if_current(4) is False
    assert manager.app_state.transcription_text == ""


def test_external_auth_error_keeps_doubao_params_and_login(monkeypatch):
    manager = _bare_manager()
    manager.awaiting_final_result = True
    auth_expired = []
    manager.on_auth_expired = lambda: auth_expired.append(True)
    cleared = []
    scheduled = []
    monkeypatch.setattr(
        transcription.ParamsStore, "clear", lambda: cleared.append(True)
    )
    monkeypatch.setattr(
        transcription.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 93,
    )

    manager._handle_auth_failure()

    assert not cleared
    assert not auth_expired
    assert manager.app_state.login_status == LoginStatus.LOGGED_IN
    assert manager.app_state.error_message == "语音服务认证失败，请检查 API 凭证"
    assert manager.audio_capture.stopped == 1
    assert manager.asr_client.disconnected == 1
    assert len(scheduled) == 1
