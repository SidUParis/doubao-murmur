"""Tests for ParamsStore."""

import json
import os

import pytest


@pytest.fixture(autouse=True)
def tmp_config_dir(tmp_path, monkeypatch):
    """Redirect config dir to a temp directory for each test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_save_and_load():
    from doubao_murmur.params_store import ASRParams, ParamsStore

    params = ASRParams(
        cookies={"sessionid": "abc123", "sid_tt": "xyz789"},
        device_id="dev_001",
        web_id="web_002",
    )
    ParamsStore.save(params)

    loaded = ParamsStore.load()
    assert loaded is not None
    assert loaded.cookies == params.cookies
    assert loaded.device_id == "dev_001"
    assert loaded.web_id == "web_002"

    from doubao_murmur.config import get_config_dir, get_params_path

    assert os.stat(get_config_dir()).st_mode & 0o777 == 0o700
    assert os.stat(get_params_path()).st_mode & 0o777 == 0o600


def test_cookie_header():
    from doubao_murmur.params_store import ASRParams

    params = ASRParams(
        cookies={"a": "1", "b": "2"},
        device_id="d",
        web_id="w",
    )
    header = params.cookie_header
    assert "a=1" in header
    assert "b=2" in header
    assert "; " in header


def test_load_nonexistent():
    from doubao_murmur.params_store import ParamsStore

    assert ParamsStore.load() is None


def test_clear():
    from doubao_murmur.params_store import ASRParams, ParamsStore

    params = ASRParams(cookies={"a": "1"}, device_id="d", web_id="w")
    ParamsStore.save(params)
    assert ParamsStore.has_saved()

    ParamsStore.clear()
    assert not ParamsStore.has_saved()
    assert ParamsStore.load() is None


def test_has_saved():
    from doubao_murmur.params_store import ParamsStore

    assert not ParamsStore.has_saved()

    from doubao_murmur.params_store import ASRParams

    ParamsStore.save(ASRParams(cookies={"a": "1"}, device_id="d", web_id="w"))
    assert ParamsStore.has_saved()


def test_load_hardens_legacy_world_readable_file():
    from doubao_murmur.config import get_config_dir, get_params_path
    from doubao_murmur.params_store import ParamsStore

    path = get_params_path()
    path.write_text(
        json.dumps({"cookies": {"a": "1"}, "device_id": "d", "web_id": "w"}),
        encoding="utf-8",
    )
    os.chmod(get_config_dir(), 0o755)
    os.chmod(path, 0o644)

    assert ParamsStore.load() is not None
    assert os.stat(get_config_dir()).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_load_refuses_symlink(tmp_path):
    from doubao_murmur.config import get_params_path
    from doubao_murmur.params_store import ParamsStore

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path = get_params_path()
    path.symlink_to(outside)

    assert ParamsStore.load() is None
    assert not ParamsStore.has_saved()
