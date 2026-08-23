"""Tests for PasteHelper."""

from unittest.mock import MagicMock, patch

from doubao_murmur.paste.paste_helper import PasteHelper, PasteTarget


def _command_candidates(*available: str):
    """Return a command_candidates side effect for tests."""
    enabled = set(available)
    return lambda tool: [[tool]] if tool in enabled else []


class TestCopyToClipboard:
    def test_wl_copy_preferred(self):
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("wl-copy"),
        ), patch("subprocess.run") as mock_run:
            PasteHelper._copy_to_clipboard("hello")
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["wl-copy"]
            assert args.kwargs["input"] == b"hello"

    def test_xclip_fallback(self):
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("xclip"),
        ), patch("subprocess.run") as mock_run:
            PasteHelper._copy_to_clipboard("hello")
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["xclip", "-selection", "clipboard"]

    def test_empty_text_ignored(self):
        with patch("subprocess.run") as mock_run:
            PasteHelper.copy_and_paste("")
            mock_run.assert_not_called()


class TestSimulatePaste:
    def test_ydotool_preferred(self):
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("ydotool"),
        ), patch.object(PasteHelper, "_paste_needs_shift",
                          return_value=False), \
             patch("subprocess.run") as mock_run:
            PasteHelper._simulate_paste()
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == [
                "ydotool", "key", "29:1", "47:1", "47:0", "29:0"
            ]

    def test_ydotool_terminal_uses_ctrl_shift_v(self):
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("ydotool"),
        ), patch.object(PasteHelper, "_paste_needs_shift",
                          return_value=True), \
             patch("subprocess.run") as mock_run:
            PasteHelper._simulate_paste()
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == [
                "ydotool", "key",
                "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"
            ]

    def test_wtype_fallback(self):
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("wtype"),
        ), patch.object(PasteHelper, "_paste_needs_shift",
                          return_value=False), \
             patch("subprocess.run") as mock_run:
            PasteHelper._simulate_paste()
            mock_run.assert_called_once()

    def test_xdotool_fallback(self):
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("xdotool"),
        ), patch.object(PasteHelper, "_paste_needs_shift",
                          return_value=False), \
             patch("subprocess.run") as mock_run:
            PasteHelper._simulate_paste()
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["xdotool", "key", "ctrl+v"]

    def test_xdotool_terminal_uses_ctrl_shift_v(self):
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("xdotool"),
        ), patch.object(PasteHelper, "_paste_needs_shift",
                          return_value=True), \
             patch("subprocess.run") as mock_run:
            PasteHelper._simulate_paste()
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["xdotool", "key", "ctrl+shift+v"]

    def test_flatpak_spawn_host_fallback(self):
        def flatpak_candidates(tool):
            if tool == "ydotool":
                return [["flatpak-spawn", "--host", "ydotool"]]
            return []

        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=flatpak_candidates,
        ), patch.object(PasteHelper, "_paste_needs_shift",
                          return_value=False), \
             patch("subprocess.run") as mock_run:
            PasteHelper._simulate_paste()
            mock_run.assert_called()
            args = mock_run.call_args
            assert args[0][0] == [
                "flatpak-spawn", "--host", "ydotool", "key",
                "29:1", "47:1", "47:0", "29:0"
            ]


class TestFocusedWindowClasses:
    def _run_detection(self, classname: bytes) -> list[str]:
        mock_result = MagicMock()
        mock_result.stdout = classname
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("xdotool"),
        ), \
             patch("subprocess.run", return_value=mock_result) as mock_run:
            result = PasteHelper._focused_window_classes()
            args = mock_run.call_args
            assert args[0][0] == [
                "xdotool", "getactivewindow", "getwindowclassname"
            ]
            return result

    def test_konsole_is_terminal(self):
        with patch.object(PasteHelper, "_focused_window_classes",
                          return_value=["konsole"]), \
             patch.object(PasteHelper, "_load_paste_overrides",
                          return_value={}):
            assert PasteHelper._paste_needs_shift() is True

    def test_browser_is_not_terminal(self):
        with patch.object(PasteHelper, "_focused_window_classes",
                          return_value=["google-chrome"]), \
             patch.object(PasteHelper, "_load_paste_overrides",
                          return_value={}):
            assert PasteHelper._paste_needs_shift() is False

    def test_case_insensitive(self):
        assert self._run_detection(b"Alacritty\n") == ["alacritty"]

    def test_warp_is_terminal(self):
        # Warp's WM class is "dev.warp.Warp" (xdotool getwindowclassname).
        with patch.object(PasteHelper, "_focused_window_classes",
                          return_value=["dev.warp.warp"]), \
             patch.object(PasteHelper, "_load_paste_overrides",
                          return_value={}):
            assert PasteHelper._paste_needs_shift() is True

    def test_no_xdotool_returns_false(self):
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            return_value=[],
        ):
            assert PasteHelper._focused_window_classes() == []
            assert PasteHelper._paste_needs_shift() is False


class TestCopyOnly:
    def test_copy_only_does_not_paste(self):
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("wl-copy"),
        ), patch("subprocess.run") as mock_run:
            PasteHelper.copy_only("text")
            # Should only be called once (for copy, not paste)
            assert mock_run.call_count == 1


class TestPasteTarget:
    def test_failed_copy_never_pastes_stale_clipboard(self):
        target = PasteTarget("42")
        with patch.object(
            PasteHelper, "_focused_window_id", return_value="42"
        ), patch.object(
            PasteHelper, "_copy_to_clipboard", return_value=False
        ) as copy, patch.object(PasteHelper, "_simulate_paste") as paste:
            assert not PasteHelper.copy_and_paste("new text", target=target)
            copy.assert_called_once_with("new text")
            paste.assert_not_called()

    def test_capture_normalizes_active_window_id(self):
        result = MagicMock(stdout=b"12345\n")
        with patch(
            "doubao_murmur.paste.paste_helper.command_candidates",
            side_effect=_command_candidates("xdotool"),
        ), patch("subprocess.run", return_value=result):
            assert PasteHelper.capture_target() == PasteTarget("12345")

    def test_matching_target_is_checked_again_before_paste(self):
        target = PasteTarget("42")
        with patch.object(
            PasteHelper, "_focused_window_id", side_effect=["42", "42"]
        ) as focused, patch.object(
            PasteHelper, "_copy_to_clipboard"
        ) as copy, patch.object(
            PasteHelper, "_simulate_paste"
        ) as paste, patch("time.sleep"):
            assert PasteHelper.copy_and_paste("text", target=target)
            assert focused.call_count == 2
            copy.assert_called_once_with("text")
            paste.assert_called_once_with()

    def test_changed_target_copies_without_pasting(self):
        target = PasteTarget("42")
        with patch.object(
            PasteHelper, "_focused_window_id", return_value="99"
        ), patch.object(
            PasteHelper, "_copy_to_clipboard"
        ) as copy, patch.object(PasteHelper, "_simulate_paste") as paste:
            assert not PasteHelper.copy_and_paste("text", target=target)
            copy.assert_called_once_with("text")
            paste.assert_not_called()

    def test_focus_change_after_copy_skips_key_injection(self):
        target = PasteTarget("42")
        with patch.object(
            PasteHelper, "_focused_window_id", side_effect=["42", "99"]
        ), patch.object(
            PasteHelper, "_copy_to_clipboard"
        ) as copy, patch.object(
            PasteHelper, "_simulate_paste"
        ) as paste, patch("time.sleep"):
            assert not PasteHelper.copy_and_paste("text", target=target)
            copy.assert_called_once_with("text")
            paste.assert_not_called()

    def test_unverifiable_target_is_clipboard_only(self):
        target = PasteTarget(None)
        with patch.object(
            PasteHelper, "_copy_to_clipboard"
        ) as copy, patch.object(PasteHelper, "_simulate_paste") as paste:
            assert not PasteHelper.copy_and_paste("text", target=target)
            copy.assert_called_once_with("text")
            paste.assert_not_called()
