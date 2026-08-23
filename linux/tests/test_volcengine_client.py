"""Offline tests for the Volcengine v3 WebSocket protocol client."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import struct

import pytest

import doubao_murmur.volcengine_client as module
from doubao_murmur.volcengine_client import (
    DEFAULT_ENDPOINT,
    DEFAULT_RESOURCE_ID,
    PendingAudioOverflowError,
    VolcengineASRClient,
    VolcengineProtocolError,
    _COMPRESSION_GZIP,
    _COMPRESSION_NONE,
    _LAST_WITH_SEQUENCE,
    _SERIALIZATION_JSON,
    _SERVER_ERROR_RESPONSE,
    _SERVER_FULL_RESPONSE,
    _build_header,
    _encode_audio_request,
    _encode_full_request,
    _extract_text,
    _looks_like_auth_error,
    _parse_server_frame,
    _websocket_header_kwargs,
)


def _server_frame(
    payload,
    *,
    sequence=1,
    final=False,
    compression=_COMPRESSION_GZIP,
):
    raw = json.dumps(payload, ensure_ascii=False).encode()
    if compression == _COMPRESSION_GZIP:
        raw = gzip.compress(raw, mtime=0)
    flags = _LAST_WITH_SEQUENCE if final else 0b0001
    return b"".join(
        (
            _build_header(
                _SERVER_FULL_RESPONSE,
                flags,
                _SERIALIZATION_JSON,
                compression,
            ),
            struct.pack(">iI", -abs(sequence) if final else sequence, len(raw)),
            raw,
        )
    )


def _error_frame(code, payload, *, compression=_COMPRESSION_NONE):
    raw = json.dumps(payload).encode()
    if compression == _COMPRESSION_GZIP:
        raw = gzip.compress(raw, mtime=0)
    return b"".join(
        (
            _build_header(
                _SERVER_ERROR_RESPONSE,
                0,
                _SERIALIZATION_JSON,
                compression,
            ),
            struct.pack(">II", code, len(raw)),
            raw,
        )
    )


def _client_frame(frame):
    flags = frame[1] & 0x0F
    compression = frame[2] & 0x0F
    offset = 4
    sequence = None
    if flags & 0b0001:
        sequence = struct.unpack(">i", frame[offset : offset + 4])[0]
        offset += 4
    payload_size = struct.unpack(">I", frame[offset : offset + 4])[0]
    offset += 4
    payload = frame[offset : offset + payload_size]
    if compression == _COMPRESSION_GZIP:
        payload = gzip.decompress(payload)
    return flags, sequence, payload


@pytest.fixture
def client():
    instance = VolcengineASRClient({"api_key": "test-api-key"})
    with instance._lock:
        instance._generation = 7
        instance._active = True
    return instance


@pytest.fixture(autouse=True)
def no_personal_vocabulary(monkeypatch):
    """Keep payload tests independent of the developer's real config."""
    monkeypatch.setattr(module, "load_personal_vocabulary", lambda: [])


class TestAuthenticationHeaders:
    def test_new_api_key_headers(self):
        client = VolcengineASRClient({"api_key": "new-secret"})

        headers = client._build_headers("connection-id")

        assert headers == {
            "X-Api-Key": "new-secret",
            "X-Api-Resource-Id": DEFAULT_RESOURCE_ID,
            "X-Api-Connect-Id": "connection-id",
            "X-Api-Sequence": "-1",
        }
        assert "X-Api-App-Key" not in headers
        assert "X-Api-Access-Key" not in headers

    def test_legacy_headers(self):
        client = VolcengineASRClient(
            {"app_id": "legacy-app", "access_token": "legacy-token"}
        )

        headers = client._build_headers("connection-id")

        assert "X-Api-Key" not in headers
        assert headers["X-Api-App-Key"] == "legacy-app"
        assert headers["X-Api-Access-Key"] == "legacy-token"

    def test_api_key_wins_if_both_auth_modes_are_present(self):
        client = VolcengineASRClient(
            {
                "api_key": "new-secret",
                "app_id": "legacy-app",
                "access_token": "legacy-token",
            }
        )
        headers = client._build_headers("id")
        assert headers["X-Api-Key"] == "new-secret"
        assert "X-Api-App-Key" not in headers

    def test_missing_credentials_reports_configuration_error(self):
        client = VolcengineASRClient({})
        errors = []
        client.on_error = errors.append

        client.connect(None)

        assert len(errors) == 1
        assert "api_key" in str(errors[0])
        assert not client.is_connected


class TestRequestPayload:
    def test_defaults_match_pcm_contract(self):
        client = VolcengineASRClient({"api_key": "x"})

        assert client._endpoint == DEFAULT_ENDPOINT
        assert client._chunk_bytes == 6_400
        assert client.is_streaming is True
        assert client.waits_for_final_event is True
        assert client.final_result_timeout == 20.0
        assert client.max_pending_audio_seconds == 10.0
        assert client.max_pending_audio_bytes == 320_000
        payload = client._build_payload()
        assert payload["audio"] == {
            "format": "pcm",
            "codec": "raw",
            "rate": 16_000,
            "bits": 16,
            "channel": 1,
        }
        assert payload["request"]["model_name"] == "bigmodel"
        assert payload["request"]["enable_itn"] is True
        assert payload["request"]["enable_punc"] is True
        assert payload["request"]["enable_ddc"] is True
        assert payload["request"]["enable_nonstream"] is True
        assert payload["request"]["end_window_size"] == 800
        assert payload["request"]["show_utterances"] is True
        assert "context" not in payload["request"]
        assert "corpus" not in payload["request"]

    def test_named_and_full_url_endpoints(self):
        named = VolcengineASRClient(
            {"api_key": "x", "endpoint": "bigmodel_async"}
        )
        nostream = VolcengineASRClient(
            {"api_key": "x", "endpoint": "bigmodel_nostream"}
        )
        custom = VolcengineASRClient(
            {"api_key": "x", "endpoint": "wss://example.test/asr"}
        )
        assert named._endpoint.endswith("/bigmodel_async")
        assert nostream._endpoint.endswith("/bigmodel_nostream")
        assert custom._endpoint == "wss://example.test/asr"
        assert named.is_streaming is True
        assert nostream.is_streaming is False
        assert nostream.waits_for_final_event is False
        assert custom.is_streaming is True
        assert "language" not in named._build_payload()["audio"]
        assert nostream._build_payload()["audio"]["language"] == "zh-CN"
        assert "enable_nonstream" not in nostream._build_payload()["request"]
        assert "language" not in custom._build_payload()["audio"]

    def test_plaintext_custom_endpoint_is_rejected(self):
        with pytest.raises(ValueError):
            VolcengineASRClient(
                {"api_key": "placeholder", "endpoint": "ws://example.test/asr"}
            )

    def test_tunable_payload_and_timeout(self):
        client = VolcengineASRClient(
            {
                "api_key": "x",
                "resource_id": "custom.resource",
                "endpoint": "bigmodel_async",
                "enable_itn": False,
                "enable_ddc": False,
                "enable_nonstream": False,
                "show_utterances": False,
                "end_window_size": 100,
                "chunk_ms": 100,
                "final_result_timeout": 7.5,
            }
        )
        assert client._resource_id == "custom.resource"
        assert client._chunk_bytes == 3_200
        assert client.final_result_timeout == 7.5
        payload = client._build_payload()
        assert "language" not in payload["audio"]
        assert payload["request"]["enable_itn"] is False
        assert payload["request"]["enable_ddc"] is False
        assert payload["request"]["enable_nonstream"] is False
        assert payload["request"]["end_window_size"] == 300

    def test_nostream_language_remains_tunable(self):
        client = VolcengineASRClient(
            {
                "api_key": "x",
                "endpoint": "bigmodel_nostream",
                "language": "fr-FR",
            }
        )
        assert client._build_payload()["audio"]["language"] == "fr-FR"

    def test_personal_vocabulary_uses_official_context_json(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            module,
            "load_personal_vocabulary",
            lambda: ["DeepSeek", "雾凇拼音"],
        )
        client = VolcengineASRClient({"api_key": "x"})

        request = client._build_payload()["request"]

        assert isinstance(request["context"], str)
        assert json.loads(request["context"]) == {
            "hotwords": [
                {"word": "DeepSeek"},
                {"word": "雾凇拼音"},
            ]
        }

    def test_personal_vocabulary_is_reloaded_for_each_request(
        self, monkeypatch
    ):
        vocabularies = iter((["first-term"], ["second-term"]))
        monkeypatch.setattr(
            module, "load_personal_vocabulary", lambda: next(vocabularies)
        )
        client = VolcengineASRClient({"api_key": "x"})

        first = json.loads(client._build_payload()["request"]["context"])
        second = json.loads(client._build_payload()["request"]["context"])

        assert first == {"hotwords": [{"word": "first-term"}]}
        assert second == {"hotwords": [{"word": "second-term"}]}

    def test_optional_managed_boosting_table_uses_corpus(self):
        client = VolcengineASRClient(
            {"api_key": "x", "boosting_table_id": "table-id-123"}
        )

        assert client._build_payload()["request"]["corpus"] == {
            "boosting_table_id": "table-id-123"
        }

    @pytest.mark.parametrize(
        ("configured", "seconds", "byte_limit"),
        [
            (0.25, 1.0, 32_000),
            (30, 30.0, 960_000),
            (90, 30.0, 960_000),
            ("invalid", 10.0, 320_000),
            (float("nan"), 10.0, 320_000),
        ],
    )
    def test_pending_audio_limit_is_bounded(
        self, configured, seconds, byte_limit
    ):
        client = VolcengineASRClient(
            {"api_key": "x", "max_pending_audio_seconds": configured}
        )

        assert client.max_pending_audio_seconds == seconds
        assert client.max_pending_audio_bytes == byte_limit


class TestPendingAudioBackpressure:
    def test_stalled_sender_buffer_is_bounded_and_reports_once(self):
        secret = "must-never-appear-in-overflow"
        client = VolcengineASRClient(
            {
                "api_key": secret,
                "max_pending_audio_seconds": 1,
            }
        )
        with client._lock:
            client._generation = 12
            client._active = True
        errors = []
        client.on_error = errors.append

        # No loop or sender consumes these bytes: this is a fully stalled
        # network path.  Filling to the exact limit is allowed.
        client.send_audio(b"x" * 32_000)
        assert len(client._pending_audio) == 32_000
        assert errors == []

        client.send_audio(b"overflow")
        client.send_audio(b"ignored after the first overflow")

        assert len(client._pending_audio) <= client.max_pending_audio_bytes
        assert len(errors) == 1
        assert isinstance(errors[0], PendingAudioOverflowError)
        assert str(errors[0]) == "Volcengine pending audio limit exceeded"
        assert secret not in str(errors[0])


class TestFrameEncoding:
    def test_full_request_is_unsequenced_uncompressed_json(self):
        frame = _encode_full_request({"audio": {"format": "pcm"}})

        assert frame[:4] == bytes((0x11, 0x10, 0x10, 0x00))
        flags, sequence, payload = _client_frame(frame)
        assert flags == 0b0000
        assert sequence is None
        assert json.loads(payload) == {"audio": {"format": "pcm"}}

    def test_regular_audio_is_unsequenced_uncompressed_pcm(self):
        frame = _encode_audio_request(b"\x01\x02" * 20)
        assert frame[:4] == bytes((0x11, 0x20, 0x00, 0x00))
        flags, sequence, payload = _client_frame(frame)
        assert flags == 0b0000
        assert sequence is None
        assert payload == b"\x01\x02" * 20

    def test_final_audio_is_unsequenced_negative_packet(self):
        frame = _encode_audio_request(b"last", final=True)
        assert frame[:4] == bytes((0x11, 0x22, 0x00, 0x00))
        flags, sequence, payload = _client_frame(frame)
        assert flags == 0b0010
        assert sequence is None
        assert payload == b"last"

    def test_legacy_sequence_encoding_remains_available(self):
        frame = _encode_audio_request(b"last", 9, final=True)
        flags, sequence, payload = _client_frame(frame)
        assert flags == _LAST_WITH_SEQUENCE
        assert sequence == -9
        assert payload == b"last"

    def test_zero_sequence_is_rejected(self):
        with pytest.raises(ValueError, match="non-zero"):
            _encode_audio_request(b"x", 0)


class TestFrameParsing:
    def test_gzip_json_result_and_final_flag(self):
        parsed = _parse_server_frame(
            _server_frame(
                {"result": {"text": "你好"}}, sequence=12, final=True
            )
        )
        assert parsed.message_type == _SERVER_FULL_RESPONSE
        assert parsed.sequence == -12
        assert parsed.is_last
        assert parsed.payload == {"result": {"text": "你好"}}

    def test_uncompressed_json_result(self):
        parsed = _parse_server_frame(
            _server_frame(
                {"result": {"text": "plain"}},
                compression=_COMPRESSION_NONE,
            )
        )
        assert parsed.payload["result"]["text"] == "plain"

    def test_error_frame(self):
        parsed = _parse_server_frame(
            _error_frame(45000001, {"message": "bad request"})
        )
        assert parsed.message_type == _SERVER_ERROR_RESPONSE
        assert parsed.error_code == 45000001
        assert parsed.is_last
        assert parsed.payload == {"message": "bad request"}

    def test_truncated_and_trailing_frames_are_rejected(self):
        with pytest.raises(ValueError, match="header"):
            _parse_server_frame(b"\x11")
        with pytest.raises(ValueError, match="unexpected bytes"):
            _parse_server_frame(_server_frame({"result": {}}) + b"junk")


class TestTextExtraction:
    def test_result_text_has_priority(self):
        assert _extract_text(
            {
                "result": {
                    "text": "完整结果",
                    "utterances": [{"text": "分句"}],
                }
            }
        ) == "完整结果"

    def test_utterances_are_joined_when_result_text_is_missing(self):
        assert _extract_text(
            {
                "result": {
                    "utterances": [
                        {"text": "第一句，"},
                        {"text": "第二句。"},
                    ]
                }
            }
        ) == "第一句，第二句。"

    def test_list_results_are_supported(self):
        assert _extract_text(
            {"result": [{"text": "one"}, {"text": "two"}]}
        ) == "onetwo"


class TestCallbacksAndCancellation:
    def test_result_then_finish_callbacks(self, client):
        events = []
        client.on_result = lambda text: events.append(("result", text))
        client.on_finish = lambda: events.append(("finish", None))

        terminal = client._handle_message(
            _server_frame(
                {"result": {"text": "最终文本"}}, final=True
            ),
            7,
        )

        assert terminal
        assert events == [("result", "最终文本"), ("finish", None)]

    def test_two_pass_result_replaces_streaming_hypothesis_before_finish(
        self, client
    ):
        events = []
        client.on_result = lambda text: events.append(("result", text))
        client.on_finish = lambda: events.append(("finish", None))

        client._handle_message(
            _server_frame(
                {
                    "result": {
                        "text": "嗯那个下周二下午三点",
                        "utterances": [
                            {
                                "text": "嗯那个下周二下午三点",
                                "definite": False,
                                "additions": {"source": "stream"},
                            }
                        ],
                    }
                },
                sequence=3,
            ),
            7,
        )
        terminal = client._handle_message(
            _server_frame(
                {
                    "result": {
                        "text": "下周二下午3点。",
                        "utterances": [
                            {
                                "text": "下周二下午3点。",
                                "definite": True,
                                "additions": {"source": "two_pass"},
                            }
                        ],
                    }
                },
                sequence=4,
                final=True,
            ),
            7,
        )

        assert terminal
        assert events == [
            ("result", "嗯那个下周二下午三点"),
            ("result", "下周二下午3点。"),
            ("finish", None),
        ]

    def test_duplicate_cumulative_text_is_not_emitted_twice(self, client):
        results = []
        client.on_result = results.append
        frame = _server_frame({"result": {"text": "same"}})
        client._handle_message(frame, 7)
        client._handle_message(frame, 7)
        assert results == ["same"]

    def test_utterance_fallback_reaches_callback(self, client):
        results = []
        client.on_result = results.append
        client._handle_message(
            _server_frame(
                {
                    "result": {
                        "utterances": [{"text": "你"}, {"text": "好"}]
                    }
                }
            ),
            7,
        )
        assert results == ["你好"]

    def test_auth_protocol_error_uses_auth_callback_only(self, client):
        auth_errors = []
        other_errors = []
        client.on_auth_error = lambda: auth_errors.append(True)
        client.on_error = other_errors.append

        assert client._handle_message(
            _error_frame(45000010, {"message": "AuthenticationError"}), 7
        )
        assert auth_errors == [True]
        assert other_errors == []

    def test_non_auth_protocol_error_uses_error_callback(self, client):
        errors = []
        client.on_error = errors.append

        assert client._handle_message(
            _error_frame(45000151, {"message": "bad audio"}), 7
        )
        assert len(errors) == 1
        assert isinstance(errors[0], VolcengineProtocolError)
        assert errors[0].code == 45000151

    def test_disconnect_invalidates_late_result(self, client):
        results = []
        client.on_result = results.append

        client.disconnect()
        terminal = client._handle_message(
            _server_frame({"result": {"text": "too late"}}, final=True),
            7,
        )

        assert terminal
        assert results == []
        assert not client.is_connected

    def test_send_audio_ignores_data_after_finish(self, client):
        client.send_audio(b"before")
        client.finish_sending()
        client.send_audio(b"after")
        assert bytes(client._pending_audio) == b"before"


class TestAudioChunking:
    def test_rechunks_pcm_and_finishes_with_negative_sequence(self, client):
        class FakeWebSocket:
            def __init__(self):
                self.sent = []

            async def send(self, frame):
                self.sent.append(frame)

        ws = FakeWebSocket()
        async def exercise():
            client._wake_event = asyncio.Event()
            client._pending_audio.extend(
                b"x" * (client._chunk_bytes * 2 + 101)
            )
            client._finish_requested = True
            await client._send_audio_stream(ws, 7)

        asyncio.run(exercise())

        decoded = [_client_frame(frame) for frame in ws.sent]
        assert [sequence for _, sequence, _ in decoded] == [None, None, None]
        assert [len(payload) for _, _, payload in decoded] == [6_400, 6_400, 101]
        assert decoded[-1][0] == 0b0010

    def test_exact_packet_is_held_and_sent_as_the_last_packet(self, client):
        class FakeWebSocket:
            def __init__(self):
                self.sent = []

            async def send(self, frame):
                self.sent.append(frame)

        ws = FakeWebSocket()
        async def exercise():
            client._wake_event = asyncio.Event()
            client._pending_audio.extend(b"x" * client._chunk_bytes)
            client._finish_requested = True
            await client._send_audio_stream(ws, 7)

        asyncio.run(exercise())

        assert len(ws.sent) == 1
        _, sequence, payload = _client_frame(ws.sent[0])
        assert sequence is None
        assert ws.sent[0][1] & 0x0F == 0b0010
        assert len(payload) == 6_400


class TestCompatibilityAndSecretSafety:
    def test_websockets_header_argument_versions(self):
        def current(*, additional_headers=None, ping_timeout=20):
            pass

        def old(*, extra_headers=None):
            pass

        assert _websocket_header_kwargs(current, {"X": "y"}) == {
            "additional_headers": {"X": "y"},
            "ping_timeout": None,
        }
        assert _websocket_header_kwargs(old, {"X": "y"}) == {
            "extra_headers": {"X": "y"}
        }

    def test_auth_error_detection_does_not_depend_on_exception_type(self):
        assert _looks_like_auth_error(VolcengineProtocolError(45000010))
        assert _looks_like_auth_error(RuntimeError("API key rejected"))
        assert not _looks_like_auth_error(RuntimeError("network unavailable"))

    def test_transport_exception_does_not_log_api_key(
        self, monkeypatch, caplog
    ):
        secret = "never-print-this-key"
        client = VolcengineASRClient({"api_key": secret})
        client._generation = 3
        client._active = True
        errors = []
        client.on_error = errors.append

        class FailedConnection:
            async def __aenter__(self):
                raise RuntimeError(f"request headers contained {secret}")

            async def __aexit__(self, *args):
                return False

        class FakeWebsockets:
            @staticmethod
            def connect(*args, extra_headers=None, **kwargs):
                return FailedConnection()

        monkeypatch.setattr(module, "_load_websockets", lambda: FakeWebsockets)
        caplog.set_level(logging.INFO)

        asyncio.run(client._connect_and_listen(3))

        assert secret not in caplog.text
        assert len(errors) == 1
        assert secret not in str(errors[0])
        assert "RuntimeError" in str(errors[0])
