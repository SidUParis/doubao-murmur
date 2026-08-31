"""AltGr disambiguation and physical edge timing tests."""

from doubao_murmur.hotkey.edge_guard import RightAltEdgeGuard


class _Timer:
    def __init__(self, seconds, callback) -> None:
        self.seconds = seconds
        self.callback = callback
        self.cancelled = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


def _guard():
    events = []
    timers = []
    now = [1_000_000_000]

    def timer_factory(seconds, callback):
        timer = _Timer(seconds, callback)
        timers.append(timer)
        return timer

    guard = RightAltEdgeGuard(
        on_press=lambda timestamp: events.append(("press", timestamp)),
        on_release=lambda timestamp: events.append(("release", timestamp)),
        on_cancel=lambda: events.append(("cancel", None)),
        timer_factory=timer_factory,
        monotonic_ns=lambda: now[0],
    )
    return guard, events, timers, now


def test_hold_forwards_original_down_then_up_timestamps() -> None:
    guard, events, timers, now = _guard()

    guard.press()
    timers[0].fire()
    now[0] = 2_000_000_000
    guard.release()

    assert events == [
        ("press", 1_000_000_000),
        ("release", 2_000_000_000),
    ]


def test_bare_tap_preserves_legacy_release_activation_order() -> None:
    guard, events, timers, now = _guard()

    guard.press()
    now[0] = 1_050_000_000
    guard.release()

    assert timers[0].cancelled
    assert events == [
        ("press", 1_000_000_000),
        ("release", 1_050_000_000),
    ]


def test_altgr_chord_inside_arming_window_emits_no_control_command() -> None:
    guard, events, timers, _ = _guard()

    guard.press()
    assert guard.other_key_pressed() is False
    timers[0].fire()
    guard.release()

    assert events == []


def test_late_chord_cancels_an_already_armed_hold_once() -> None:
    guard, events, timers, _ = _guard()

    guard.press()
    timers[0].fire()
    assert guard.other_key_pressed() is True
    assert guard.other_key_pressed() is False
    guard.release()

    assert events == [("press", 1_000_000_000), ("cancel", None)]


def test_repeat_key_down_is_idempotent() -> None:
    guard, events, timers, _ = _guard()

    guard.press()
    guard.press()
    assert len(timers) == 1
    timers[0].fire()
    guard.release()

    assert [event[0] for event in events] == ["press", "release"]


def test_timer_failure_fails_closed_without_emitting_an_edge() -> None:
    events = []
    guard = RightAltEdgeGuard(
        on_press=lambda timestamp: events.append(("press", timestamp)),
        on_release=lambda timestamp: events.append(("release", timestamp)),
        on_cancel=lambda: events.append(("cancel", None)),
        timer_factory=lambda _seconds, _callback: (_ for _ in ()).throw(
            RuntimeError("timer unavailable")
        ),
    )

    assert guard.press() is False
    guard.release()
    assert events == []
