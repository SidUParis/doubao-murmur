"""Visual-state tests for the compact push-to-talk indicator."""

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


def test_error_state_uses_warning_indicator_and_message():
    indicator = _indicator()

    indicator.set_error("麦克风启动失败")

    assert indicator._button.label == "⚠"
    assert "error" in indicator._button.classes
    assert indicator._button.tooltip == "麦克风启动失败"
