"""Tests for config module."""

import json
import logging
import stat

import pytest

from doubao_murmur.config import (
    AUDIO_BLOCKSIZE,
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    AUTH_ERROR_CODE,
    AUTH_ERROR_KEYWORDS,
    MAX_PERSONAL_VOCABULARY_TERM_LENGTH,
    MAX_PERSONAL_VOCABULARY_TERMS,
    MAX_VOLCENGINE_API_KEY_LENGTH,
    VOLCENGINE_RECOMMENDED_BACKEND,
    WSS_BASE_URL,
    get_backend_config_path,
    get_config_dir,
    get_params_path,
    get_personal_vocabulary_path,
    get_volcengine_config_path,
    has_volcengine_api_key,
    load_personal_vocabulary,
    load_volcengine_config,
    save_personal_vocabulary,
    save_volcengine_api_key,
)


def test_wss_url():
    assert WSS_BASE_URL == "wss://ws-samantha.doubao.com/samantha/audio/asr"


def test_audio_params():
    assert AUDIO_SAMPLE_RATE == 16000
    assert AUDIO_CHANNELS == 1
    assert AUDIO_BLOCKSIZE == 4096


def test_auth_error_code():
    assert AUTH_ERROR_CODE == 709599054
    assert "cookie" in AUTH_ERROR_KEYWORDS
    assert "auth" in AUTH_ERROR_KEYWORDS
    assert "session" in AUTH_ERROR_KEYWORDS


def test_get_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config_dir = get_config_dir()
    assert config_dir == tmp_path / "doubao-murmur"
    assert config_dir.exists()


def test_get_params_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = get_params_path()
    assert path == tmp_path / "doubao-murmur" / "asr_params.json"


def test_get_volcengine_config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = get_volcengine_config_path()
    assert path == tmp_path / "doubao-murmur" / "volcengine.json"


def test_load_volcengine_config_merges_only_safe_backend_overrides(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    get_volcengine_config_path().write_text(
        '{"api_key":"local-secret","language":"zh-CN",'
        '"resource_id":"local-resource"}',
        encoding="utf-8",
    )

    settings = load_volcengine_config(
        {
            "backend": "volcengine",
            "api_key": "must-not-win",
            "app_id": "must-not-leak-in",
            "access_key": "must-not-leak-in",
            "endpoint": "bigmodel_nostream",
            "language": "en-US",
            "chunk_ms": 160,
        }
    )

    assert settings == {
        "api_key": "local-secret",
        "language": "en-US",
        "resource_id": "local-resource",
        "endpoint": "bigmodel_nostream",
        "chunk_ms": 160,
    }


def test_load_volcengine_config_survives_bad_secret_file(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    get_volcengine_config_path().write_text("not json", encoding="utf-8")

    assert load_volcengine_config(
        {"backend": "volcengine", "language": "zh-CN"}
    ) == {"language": "zh-CN"}


def test_load_volcengine_config_reads_backend_file_overrides(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    get_volcengine_config_path().write_text(
        '{"api_key":"local-secret","language":"zh-CN"}',
        encoding="utf-8",
    )
    get_backend_config_path().write_text(
        '{"backend":"volcengine","language":"en-US",'
        '"final_result_timeout":12,"boosting_table_id":"table-123"}',
        encoding="utf-8",
    )

    assert load_volcengine_config() == {
        "api_key": "local-secret",
        "language": "en-US",
        "final_result_timeout": 12,
        "boosting_table_id": "table-123",
    }


def test_save_volcengine_api_key_writes_private_atomic_profile(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    tmp_path.chmod(0o755)
    key = "test-key-that-must-stay-secret"

    save_volcengine_api_key(f"  {key}  ")

    credential_path = get_volcengine_config_path()
    backend_path = get_backend_config_path()
    assert json.loads(credential_path.read_text(encoding="utf-8")) == {
        "api_key": key
    }
    backend = json.loads(backend_path.read_text(encoding="utf-8"))
    assert backend == VOLCENGINE_RECOMMENDED_BACKEND
    assert backend["max_pending_audio_seconds"] == 10
    assert backend["max_recording_seconds"] == 600
    assert key not in backend_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    assert stat.S_IMODE(credential_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backend_path.stat().st_mode) == 0o600
    assert not list(credential_path.parent.glob(".*.tmp"))
    assert has_volcengine_api_key() is True


@pytest.mark.parametrize(
    "api_key",
    [
        "",
        "   ",
        "line-one\nline-two",
        "x" * (MAX_VOLCENGINE_API_KEY_LENGTH + 1),
        None,
    ],
)
def test_save_volcengine_api_key_rejects_invalid_values(
    tmp_path, monkeypatch, api_key
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ValueError):
        save_volcengine_api_key(api_key)

    assert not get_volcengine_config_path().exists()
    assert not get_backend_config_path().exists()


def test_personal_vocabulary_round_trip_is_normalized_and_private(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    saved = save_personal_vocabulary(
        "  DeepSeek  \n豆包\n\n deepseek\nMurmur IME  "
    )

    assert saved == ["DeepSeek", "豆包", "Murmur IME"]
    assert load_personal_vocabulary() == saved
    path = get_personal_vocabulary_path()
    assert path.name == "personal_vocabulary.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"terms": saved}
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".*.tmp"))


def test_personal_vocabulary_loader_sanitizes_individual_terms(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    get_personal_vocabulary_path().write_text(
        json.dumps(
            {
                "terms": [
                    "  Codex  ",
                    42,
                    "codex",
                    "x" * (MAX_PERSONAL_VOCABULARY_TERM_LENGTH + 1),
                    "雾凇拼音",
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_personal_vocabulary() == ["Codex", "雾凇拼音"]


@pytest.mark.parametrize(
    "terms",
    [
        ["x" * (MAX_PERSONAL_VOCABULARY_TERM_LENGTH + 1)],
        [f"term-{index}" for index in range(MAX_PERSONAL_VOCABULARY_TERMS + 1)],
        ["valid", 42],
    ],
)
def test_personal_vocabulary_save_rejects_limits_without_overwriting(
    tmp_path, monkeypatch, terms
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_personal_vocabulary(["existing"])

    with pytest.raises(ValueError):
        save_personal_vocabulary(terms)

    assert load_personal_vocabulary() == ["existing"]


def test_personal_vocabulary_read_error_never_logs_terms(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    secret_term = "private-customer-project"
    get_personal_vocabulary_path().write_text(
        f'{{"terms":["{secret_term}"]', encoding="utf-8"
    )
    caplog.set_level(logging.WARNING)

    assert load_personal_vocabulary() == []

    assert secret_term not in caplog.text
    assert "JSONDecodeError" in caplog.text
