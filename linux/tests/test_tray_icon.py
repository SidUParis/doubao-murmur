"""Tests for backend-aware tray and control-panel actions."""

import logging
from types import SimpleNamespace

from doubao_murmur.app_state import LoginStatus, RecordingState
from doubao_murmur.ui import tray_icon as tray_icon_module
from doubao_murmur.ui.tray_icon import TrayIcon


class _Label:
    def __init__(self):
        self.text = None

    def set_text(self, text):
        self.text = text


class _Button:
    def __init__(self):
        self.visible = None
        self.label = None

    def set_visible(self, visible):
        self.visible = visible

    def set_label(self, label):
        self.label = label


class _Entry:
    def __init__(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text


class _TextBuffer:
    def __init__(self, text):
        self.text = text

    def get_start_iter(self):
        return "start"

    def get_end_iter(self):
        return "end"

    def get_text(self, start, end, include_hidden_chars):
        assert (start, end, include_hidden_chars) == ("start", "end", True)
        return self.text

    def set_text(self, text):
        self.text = text


class _TextView:
    def __init__(self, text):
        self.buffer = _TextBuffer(text)

    def get_buffer(self):
        return self.buffer


def _external_tray():
    tray = TrayIcon.__new__(TrayIcon)
    tray.app_state = SimpleNamespace(
        login_status=LoginStatus.LOGGED_IN,
        recording_state=RecordingState.IDLE,
    )
    tray._uses_doubao_login = False
    tray._on_login_clicked = lambda: None
    tray._on_logout_clicked = lambda: None
    tray._on_quit_clicked = lambda: None
    tray._on_help_clicked = None
    tray._on_keyboard_clicked = None
    tray._on_backend_reload = lambda: True
    tray._sni = None
    tray._status_label = _Label()
    tray._primary_button = _Button()
    tray._volcengine_key_entry = _Entry("")
    tray._volcengine_message_label = _Label()
    tray._personal_vocabulary_view = _TextView("")
    tray._personal_vocabulary_message_label = _Label()
    return tray


def test_external_backend_menu_has_no_doubao_auth_action():
    tray = _external_tray()

    labels = [item["label"] for item in tray._menu_items() if item]

    assert not any("登录" in label for label in labels)
    assert "控制面板" in labels


def test_external_backend_hides_control_panel_auth_button():
    tray = _external_tray()

    tray._refresh()

    assert tray._status_label.text == "状态：语音后端已配置"
    assert tray._primary_button.visible is False
    assert tray._primary_button.label is None


def test_sni_uses_visible_open_voice_input_brand(monkeypatch):
    tray = _external_tray()
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


def test_saving_volcengine_key_clears_masked_field(monkeypatch):
    tray = _external_tray()
    secret = "secret-only-for-this-test"
    tray._volcengine_key_entry = _Entry(secret)
    received = []
    monkeypatch.setattr(
        tray_icon_module,
        "save_volcengine_api_key",
        lambda value: received.append(value),
    )

    tray._on_save_volcengine_clicked(None)

    assert received == [secret]
    assert tray._volcengine_key_entry.text == ""
    assert tray._volcengine_message_label.text == (
        "已保存并启用；下一次录音生效。"
    )


def test_saving_key_while_busy_explains_deferred_reload(monkeypatch):
    tray = _external_tray()
    tray.app_state.recording_state = RecordingState.RECORDING
    tray._volcengine_key_entry = _Entry("busy-session-key")
    reloads = []
    tray._on_backend_reload = lambda: reloads.append(True) and False
    monkeypatch.setattr(
        tray_icon_module, "save_volcengine_api_key", lambda _value: None
    )

    tray._on_save_volcengine_clicked(None)

    assert reloads == [True]
    assert tray._volcengine_key_entry.text == ""
    assert tray._volcengine_message_label.text == (
        "API Key 已安全保存；请停止当前录音后再次保存，或重启应用生效。"
    )


def test_idle_reload_failure_requests_restart(monkeypatch):
    tray = _external_tray()
    tray._volcengine_key_entry = _Entry("idle-reload-key")
    tray._on_backend_reload = lambda: False
    monkeypatch.setattr(
        tray_icon_module, "save_volcengine_api_key", lambda _value: None
    )

    tray._on_save_volcengine_clicked(None)

    assert tray._volcengine_message_label.text == (
        "API Key 已安全保存；后端重载失败，请重启应用生效。"
    )


def test_backend_login_mode_setter_refreshes_controls():
    tray = _external_tray()
    refreshes = []
    tray._refresh = lambda: refreshes.append(True)

    tray.set_uses_doubao_login(True)

    assert tray._uses_doubao_login is True
    assert refreshes == [True]


def test_saving_volcengine_key_never_logs_secret(
    monkeypatch, caplog
):
    tray = _external_tray()
    secret = "secret-that-an-exception-might-repeat"
    tray._volcengine_key_entry = _Entry(secret)

    def fail_with_secret(value):
        raise OSError(f"cannot write {value}")

    monkeypatch.setattr(
        tray_icon_module, "save_volcengine_api_key", fail_with_secret
    )
    caplog.set_level(logging.ERROR)

    tray._on_save_volcengine_clicked(None)

    assert secret not in caplog.text
    assert "OSError" in caplog.text
    assert tray._volcengine_message_label.text == (
        "保存失败，请检查配置目录权限后重试。"
    )


def test_personal_vocabulary_ui_discloses_remote_use_and_no_learning():
    disclosure = tray_icon_module._PERSONAL_VOCABULARY_DISCLOSURE

    assert "每次语音请求发送给火山引擎" in disclosure
    assert "不会读取剪贴板或输入历史" in disclosure


def test_saving_personal_vocabulary_normalizes_editor(monkeypatch):
    tray = _external_tray()
    raw = "  DeepSeek  \n豆包\ndeepseek"
    tray._personal_vocabulary_view = _TextView(raw)
    received = []
    reloads = []
    tray._on_backend_reload = lambda: reloads.append(True) or True

    def save(value):
        received.append(value)
        return ["DeepSeek", "豆包"]

    monkeypatch.setattr(tray_icon_module, "save_personal_vocabulary", save)

    tray._on_save_personal_vocabulary_clicked(None)

    assert received == [raw]
    assert tray._personal_vocabulary_view.buffer.text == "DeepSeek\n豆包"
    assert tray._personal_vocabulary_message_label.text == (
        "已保存 2 个术语；下一次录音生效。"
    )
    assert reloads == []


def test_saving_personal_vocabulary_never_logs_terms(monkeypatch, caplog):
    tray = _external_tray()
    private_term = "confidential-customer-name"
    tray._personal_vocabulary_view = _TextView(private_term)

    def fail_with_term(value):
        raise OSError(f"cannot write {value}")

    monkeypatch.setattr(
        tray_icon_module, "save_personal_vocabulary", fail_with_term
    )
    caplog.set_level(logging.ERROR)

    tray._on_save_personal_vocabulary_clicked(None)

    assert private_term not in caplog.text
    assert "OSError" in caplog.text
    assert tray._personal_vocabulary_message_label.text == (
        "词表保存失败，请检查配置目录权限后重试。"
    )
