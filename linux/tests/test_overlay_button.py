"""Visual-state tests for the compact push-to-talk indicator."""

from doubao_murmur.app_state import (
    CLIPBOARD_ARMED_NOTICE,
    CLIPBOARD_READY_NOTICE,
    StatusNotice,
)
from doubao_murmur.hotkey.overlay_button import OverlayButton


class _Button:
    def __init__(self):
        self.classes = set()
        self.label = ""
        self.tooltip = ""

    def add_css_class(self, name):
        self.classes.add(name)

    def remove_css_class(self, name):
        self.classes.discard(name)

    def set_label(self, label):
        self.label = label

    def set_tooltip_text(self, tooltip):
        self.tooltip = tooltip


def _indicator():
    indicator = OverlayButton(lambda: None, lambda: None)
    indicator._button = _Button()
    return indicator


def test_recording_state_uses_stop_indicator():
    indicator = _indicator()

    indicator.set_state("recording")

    assert indicator._button.label == "⏹"
    assert "recording" in indicator._button.classes


def test_stopping_state_uses_finalizing_indicator():
    indicator = _indicator()

    indicator.set_state("stopping")

    assert indicator._button.label == "✨"
    assert "finalizing" in indicator._button.classes
    assert "二遍识别" in indicator._button.tooltip


def test_observing_state_is_not_misreported_as_idle_or_recording():
    indicator = _indicator()

    indicator.set_state("observing")

    assert indicator._button.label == "✓"
    assert "五秒" in indicator._button.tooltip
    assert "recording" not in indicator._button.classes
    assert not indicator._recording


def test_error_state_uses_warning_indicator_and_message():
    indicator = _indicator()

    indicator.set_error("麦克风启动失败")

    assert indicator._button.label == "⚠"
    assert "error" in indicator._button.classes
    assert indicator._button.tooltip == "麦克风启动失败"


def test_successful_state_then_clear_error_removes_warning():
    indicator = _indicator()
    indicator.set_error("旧错误")

    indicator.set_state("idle")
    indicator.clear_error()

    assert indicator._button.label == "🎤"
    assert "error" not in indicator._button.classes


def test_clipboard_armed_is_visible_while_idle_without_error_styling():
    indicator = _indicator()
    positions = []
    indicator._apply_position = lambda tucked: positions.append(tucked)

    indicator.set_notice(StatusNotice.CLIPBOARD_ARMED.value)

    assert indicator._button.label == "📋"
    assert indicator._button.tooltip == CLIPBOARD_ARMED_NOTICE
    assert "notice" in indicator._button.classes
    assert "ready" not in indicator._button.classes
    assert "error" not in indicator._button.classes
    assert indicator._busy()
    assert positions == [False]


def test_clipboard_ready_uses_historical_wording_and_distinct_style():
    indicator = _indicator()

    indicator.set_notice(StatusNotice.CLIPBOARD_READY.value)

    assert indicator._button.label == "✓"
    assert indicator._button.tooltip == (
        "上一条终稿已复制，可在远端手动粘贴；可能已被覆盖"
    )
    assert indicator._button.tooltip == CLIPBOARD_READY_NOTICE
    assert "ready" in indicator._button.classes
    assert "notice" not in indicator._button.classes
    assert "error" not in indicator._button.classes


def test_error_and_active_state_keep_precedence_over_clipboard_notice():
    indicator = _indicator()
    indicator.set_notice(StatusNotice.CLIPBOARD_READY.value)

    indicator.set_state("recording")
    assert indicator._button.label == "⏹"
    assert "recording" in indicator._button.classes

    indicator.set_error("麦克风启动失败")
    assert indicator._button.label == "⚠"
    assert "error" in indicator._button.classes

    indicator.set_state("idle")
    indicator.clear_error()
    assert indicator._button.label == "✓"
    assert "ready" in indicator._button.classes


def test_existing_error_keeps_precedence_when_active_state_arrives_later():
    indicator = _indicator()
    indicator.set_error("麦克风启动失败")

    indicator.set_state("recording")

    assert indicator._button.label == "⚠"
    assert indicator._button.tooltip == "麦克风启动失败"
    assert "error" in indicator._button.classes
    assert "recording" not in indicator._button.classes


def test_clearing_clipboard_notice_restores_idle_edge_tucking():
    indicator = _indicator()
    positions = []
    indicator._apply_position = lambda tucked: positions.append(tucked)
    indicator.set_notice(StatusNotice.CLIPBOARD_ARMED.value)

    indicator.set_notice(StatusNotice.NONE.value)

    assert indicator._button.label == "🎤"
    assert not indicator._busy()
    assert positions == [False, True]
