"""Right-Alt semantics independent of a live X11 connection."""

from doubao_murmur.hotkey.x11_listener import X11KeyListener


def _listener():
    edges = []
    cancels = []
    listener = X11KeyListener(
        on_press=lambda timestamp: edges.append(("press", timestamp)),
        on_release=lambda timestamp: edges.append(("release", timestamp)),
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
    return listener, edges, cancels


def test_right_alt_press_release_forwards_two_ordered_edges():
    listener, edges, _ = _listener()

    listener._handle_key(108, True)
    listener._handle_key(108, False)

    assert [edge[0] for edge in edges] == ["press", "release"]
    assert edges[1][1] >= edges[0][1]


def test_right_alt_chord_does_not_emit_edges():
    listener, edges, _ = _listener()

    listener._handle_key(108, True)
    listener._handle_key(38, True)
    listener._handle_key(38, False)
    listener._handle_key(108, False)

    assert edges == []


def test_escape_dispatches_cancel():
    listener, _, cancels = _listener()

    listener._handle_key(9, True)

    assert cancels == [True]
