"""Capability and metadata regressions for the controller-only Flatpak."""

import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from doubao_murmur import __version__


MANIFEST = Path(__file__).resolve().parents[1] / "flatpak" / "com.doubao.Murmur.yml"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
METAINFO = (
    Path(__file__).resolve().parents[1] / "flatpak" / "com.doubao.Murmur.metainfo.xml"
)


def test_controller_release_version_is_consistent():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    release = ET.parse(METAINFO).getroot().find("./releases/release")

    assert project["project"]["version"] == "1.6.1"
    assert __version__ == "1.6.1"
    assert release is not None
    assert release.attrib["version"] == "1.6.1"


def test_controller_can_only_see_private_runtime_socket_read_only():
    text = MANIFEST.read_text(encoding="utf-8")

    assert "--filesystem=xdg-run/murmur-ime:ro" in text
    assert "--filesystem=xdg-config/doubao-murmur:create" not in text
    assert "--talk-name=org.kde.StatusNotifierWatcher" in text


def test_flatpak_has_no_microphone_network_preedit_or_host_spawn_capability():
    text = MANIFEST.read_text(encoding="utf-8")
    runtime_permissions = text.split("modules:", 1)[0]

    forbidden = (
        "--socket=pulseaudio",
        "--share=network",
        "--talk-name=org.murmur.IME.Preedit1",
        "--talk-name=org.freedesktop.Flatpak",
        "--talk-name=org.freedesktop.portal.Desktop",
        "--talk-name=org.freedesktop.Notifications",
    )
    for capability in forbidden:
        assert capability not in runtime_permissions


def test_flatpak_build_has_no_local_asr_or_audio_dependency():
    text = MANIFEST.read_text(encoding="utf-8")

    assert "python-xlib==0.33" in text
    assert "six==1.17.0" in text
    for dependency in ("portaudio", "sounddevice", "websockets"):
        assert dependency not in text.lower()


def test_flatpak_installs_an_explicit_controller_module_allowlist():
    text = MANIFEST.read_text(encoding="utf-8")

    assert "cp -r src/doubao_murmur" not in text
    for forbidden in (
        "audio_capture.py",
        "asr_client.py",
        "config.py",
        "params_store.py",
        "preedit_client.py",
        "transcription.py",
        "volcengine_client.py",
        "paste_helper.py",
        "keyboard_window.py",
        "login_window.py",
    ):
        assert (
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(forbidden)}(?![A-Za-z0-9_])",
                text,
            )
            is None
        )
