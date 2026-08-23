"""Privacy and persistence tests for the optional legacy clipboard glossary."""

import json
import os

import pytest


@pytest.fixture(autouse=True)
def tmp_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_harvest_saves_private_atomic_file():
    from doubao_murmur.config import get_config_dir, get_glossary_path
    from doubao_murmur.glossary import Glossary

    glossary = Glossary()
    assert glossary.harvest("OpenVoiceInput") == 1

    assert os.stat(get_config_dir()).st_mode & 0o777 == 0o700
    assert os.stat(get_glossary_path()).st_mode & 0o777 == 0o600
    assert not list(get_config_dir().glob(".glossary.json.*.tmp"))


def test_load_hardens_legacy_glossary_permissions():
    from doubao_murmur.config import get_config_dir, get_glossary_path
    from doubao_murmur.glossary import Glossary

    path = get_glossary_path()
    path.write_text(
        json.dumps(
            {"pinned": ["OpenVoiceInput"], "blocked": [], "learned": {}}
        ),
        encoding="utf-8",
    )
    os.chmod(get_config_dir(), 0o755)
    os.chmod(path, 0o644)

    glossary = Glossary()
    assert glossary.terms() == ["OpenVoiceInput"]
    assert os.stat(get_config_dir()).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_load_refuses_symlinked_glossary(tmp_path):
    from doubao_murmur.config import get_glossary_path
    from doubao_murmur.glossary import Glossary

    outside = tmp_path / "outside-glossary.json"
    outside.write_text(
        json.dumps({"pinned": ["must-not-load"], "blocked": [], "learned": {}}),
        encoding="utf-8",
    )
    get_glossary_path().symlink_to(outside)

    assert Glossary().terms() == []
