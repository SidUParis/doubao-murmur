"""Tests for the provider-free controller tray."""

from types import SimpleNamespace

from doubao_murmur.app_state import RecordingState
from doubao_murmur.ui import tray_icon as tray_icon_module
from doubao_murmur.ui.tray_icon import TrayIcon


class _Label:
    def __init__(self):
        self.text = None

    def set_text(self, text):
        self.text = text


class _Sni:
    def __init__(self):
        self.menu = None
        self.tooltip = None

    def set_menu(self, menu):
        self.menu = menu

    def set_tooltip(self, tooltip):
        self.tooltip = tooltip


def _tray(state=RecordingState.IDLE, error=None):
    tray = TrayIcon.__new__(TrayIcon)
    tray.app_state = SimpleNamespace(
        recording_state=state,
        error_message=error,
    )
    tray._on_quit_clicked = lambda: None
    tray._on_help_clicked = lambda: None
    tray._sni = None
    tray._window = None
    tray._status_label = _Label()
    return tray


def test_menu_contains_status_help_and_quit_but_no_provider_settings():
    tray = _tray(RecordingState.RECORDING)

    labels = [item["label"] for item in tray._menu_items() if item]

    assert "状态：正在录音" in labels
    assert "状态" in labels
    assert "使用帮助" in labels
    assert "退出兼容界面" in labels
    assert not any(
        "登录" in label or "API" in label or "词表" in label for label in labels
    )


def test_observation_and_error_are_visible_in_status():
    tray = _tray(RecordingState.OBSERVING)
    assert "观察原位纠错" in tray._status_text()

    tray.app_state.error_message = "独立语音服务不可用"
    assert tray._status_text() == "状态：⚠ 独立语音服务不可用"


def test_refresh_updates_window_and_sni():
    tray = _tray(RecordingState.STOPPING)
    tray._sni = _Sni()

    tray._refresh()

    assert tray._status_label.text == "状态：正在等待最终结果"
    assert tray._sni.tooltip == tray._status_label.text
    assert tray._sni.menu is not None


def test_sni_uses_visible_open_voice_input_brand(monkeypatch):
    tray = _tray()
    captured = {}

    class FakeSniTray:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            return False

    monkeypatch.setattr(tray_icon_module, "SniTray", FakeSniTray)

    tray._start_sni()

    assert captured["item_id"] == "doubao-murmur"
    assert captured["title"] == "Open Voice Input Linux"
    assert captured["tooltip"] == "Open Voice Input Linux · 语音输入"


def test_tray_module_has_no_credential_or_vocabulary_writer():
    for name in (
        "save_volcengine_api_key",
        "save_personal_vocabulary",
        "ParamsStore",
        "LoginWindow",
    ):
        assert not hasattr(tray_icon_module, name)
