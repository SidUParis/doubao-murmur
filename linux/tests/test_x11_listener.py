"""Right-Alt semantics independent of a live X11 connection."""

from doubao_murmur.hotkey.x11_listener import X11KeyListener


def _listener():
    toggles = []
    cancels = []
    listener = X11KeyListener(
        on_toggle=lambda: toggles.append(True),
        on_escape=lambda: cancels.append(True),
    )
    listener._kc_toggle = frozenset({108})
    listener._kc_escape = 9
    listener._kc_ctrl_l = 37
    listener._kc_ctrl_r = 105
    listener._kc_super_l = 133
    listener._kc_super_r = 134
    listener._kc_shift_l = 50
    listener._kc_shift_r = 62
    return listener, toggles, cancels


def test_right_alt_press_release_toggles_exactly_once():
    listener, toggles, _ = _listener()

    listener._handle_key(108, True)
    listener._handle_key(108, False)

    assert toggles == [True]


def test_right_alt_chord_does_not_toggle():
    listener, toggles, _ = _listener()

    listener._handle_key(108, True)
    listener._handle_key(38, True)
    listener._handle_key(38, False)
    listener._handle_key(108, False)

    assert toggles == []


def test_escape_dispatches_cancel():
    listener, _, cancels = _listener()

    listener._handle_key(9, True)

    assert cancels == [True]
