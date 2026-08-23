"""Streaming client for Volcengine's v3 big-model ASR protocol.

The public surface intentionally matches :class:`ASRClient`: GTK starts an
utterance with ``connect(None)``, audio capture calls ``send_audio`` from its
own thread, and ``finish_sending`` emits the required final negative packet
without closing the socket before the final transcript arrives.

Volcengine uses a small binary envelope inside WebSocket binary messages.  The
protocol version nibble is currently 1 even though the service URL is ``v3``.
All integer fields are network byte order.  The current API-key endpoint uses
uncompressed request payloads without per-frame sequence fields; the decoder
also accepts legacy sequenced and gzip-compressed server responses.
"""

from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import logging
import math
import struct
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from doubao_murmur.config import load_personal_vocabulary

logger = logging.getLogger(__name__)

_HOST = "wss://openspeech.bytedance.com/api/v3/sauc"
_ENDPOINTS = {
    "bigmodel": f"{_HOST}/bigmodel",
    "bigmodel_async": f"{_HOST}/bigmodel_async",
    "bigmodel_nostream": f"{_HOST}/bigmodel_nostream",
}
# The optimized bidirectional endpoint gives an input-method UI both useful
# live hypotheses and one authoritative two-pass result after the utterance.
DEFAULT_ENDPOINT = _ENDPOINTS["bigmodel_async"]
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"

_SAMPLE_RATE = 16_000
_SAMPLE_BITS = 16
_CHANNELS = 1
_BYTES_PER_SAMPLE = _SAMPLE_BITS // 8
_DEFAULT_PENDING_AUDIO_SECONDS = 10.0
_MIN_PENDING_AUDIO_SECONDS = 1.0
_MAX_PENDING_AUDIO_SECONDS = 30.0

# Binary protocol nibbles.
_VERSION = 0b0001
_HEADER_WORDS = 0b0001
_CLIENT_FULL_REQUEST = 0b0001
_CLIENT_AUDIO_REQUEST = 0b0010
_SERVER_FULL_RESPONSE = 0b1001
_SERVER_ERROR_RESPONSE = 0b1111

_NO_SEQUENCE = 0b0000
_POSITIVE_SEQUENCE = 0b0001
_LAST_NO_SEQUENCE = 0b0010
_LAST_WITH_SEQUENCE = 0b0011

_SERIALIZATION_NONE = 0b0000
_SERIALIZATION_JSON = 0b0001
_COMPRESSION_NONE = 0b0000
_COMPRESSION_GZIP = 0b0001

_AUTH_ERROR_CODES = {
    401,
    403,
    45000010,  # AuthenticationError used by the v3 gateway.
    45000011,
    45000012,
}
_AUTH_WORDS = (
    "auth",
    "unauthor",
    "forbidden",
    "api key",
    "access key",
    "credential",
    "permission",
)


class VolcengineProtocolError(RuntimeError):
    """A server error frame whose string form never includes response data."""

    def __init__(self, code: int, detail: Any = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"Volcengine ASR protocol error {code}")


class PendingAudioOverflowError(RuntimeError):
    """Fixed, content-free signal that network backpressure hit its limit."""

    def __init__(self) -> None:
        super().__init__("Volcengine pending audio limit exceeded")


@dataclass(frozen=True)
class ParsedFrame:
    """Decoded server frame, retained as a small testable protocol boundary."""

    message_type: int
    flags: int
    serialization: int
    compression: int
    sequence: int | None
    is_last: bool
    payload: Any = None
    error_code: int | None = None


def _build_header(
    message_type: int,
    flags: int,
    serialization: int,
    compression: int,
) -> bytes:
    return bytes(
        (
            (_VERSION << 4) | _HEADER_WORDS,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0,
        )
    )


def _gzip(data: bytes) -> bytes:
    """Produce deterministic gzip bytes (useful for offline frame tests)."""
    return gzip.compress(data, mtime=0)


def _encode_full_request(
    payload: dict[str, Any], sequence: int | None = None
) -> bytes:
    """Encode the initial uncompressed JSON request.

    ``sequence`` remains optional for compatibility with older deployments;
    current v3 API-key sessions omit it.
    """
    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    flags = _POSITIVE_SEQUENCE if sequence is not None else _NO_SEQUENCE
    parts = [
        _build_header(
            _CLIENT_FULL_REQUEST,
            flags,
            _SERIALIZATION_JSON,
            _COMPRESSION_NONE,
        )
    ]
    if sequence is not None:
        if sequence == 0:
            raise ValueError("sequence must be non-zero")
        parts.append(struct.pack(">i", abs(sequence)))
    parts.extend((struct.pack(">I", len(body)), body))
    return b"".join(parts)


def _encode_audio_request(
    audio: bytes, sequence: int | None = None, *, final: bool = False
) -> bytes:
    """Encode raw PCM and mark the final, protocol-named "negative packet".

    In the current wire format a negative packet is the last-packet flag
    ``0x2`` and has no sequence field.  Passing ``sequence`` keeps the legacy
    ``0x3`` plus negative-int representation available for compatible servers.
    """
    if sequence == 0:
        raise ValueError("sequence must be non-zero")
    if sequence is None:
        flags = _LAST_NO_SEQUENCE if final else _NO_SEQUENCE
    else:
        flags = _LAST_WITH_SEQUENCE if final else _POSITIVE_SEQUENCE
    body = bytes(audio)
    parts = [
        _build_header(
            _CLIENT_AUDIO_REQUEST,
            flags,
            _SERIALIZATION_NONE,
            _COMPRESSION_NONE,
        )
    ]
    if sequence is not None:
        wire_sequence = -abs(sequence) if final else abs(sequence)
        parts.append(struct.pack(">i", wire_sequence))
    parts.extend((struct.pack(">I", len(body)), body))
    return b"".join(parts)


def _decode_payload(serialization: int, compression: int, payload: bytes) -> Any:
    if compression == _COMPRESSION_GZIP:
        payload = gzip.decompress(payload)
    elif compression != _COMPRESSION_NONE:
        raise ValueError(f"unsupported payload compression {compression}")

    if serialization == _SERIALIZATION_JSON:
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))
    if serialization == _SERIALIZATION_NONE:
        return payload
    raise ValueError(f"unsupported payload serialization {serialization}")


def _take(frame: bytes, offset: int, size: int, label: str) -> tuple[bytes, int]:
    end = offset + size
    if offset < 0 or end > len(frame):
        raise ValueError(f"truncated Volcengine frame ({label})")
    return frame[offset:end], end


def _parse_server_frame(message: bytes | bytearray | memoryview) -> ParsedFrame:
    """Parse and validate a Volcengine full-response or error frame."""
    frame = bytes(message)
    if len(frame) < 4:
        raise ValueError("truncated Volcengine frame header")

    version = frame[0] >> 4
    header_words = frame[0] & 0x0F
    if version != _VERSION:
        raise ValueError(f"unsupported Volcengine protocol version {version}")
    if header_words < 1:
        raise ValueError("invalid Volcengine header size")

    header_size = header_words * 4
    if len(frame) < header_size:
        raise ValueError("truncated Volcengine extended header")
    message_type = frame[1] >> 4
    flags = frame[1] & 0x0F
    serialization = frame[2] >> 4
    compression = frame[2] & 0x0F
    offset = header_size

    sequence = None
    if flags & _POSITIVE_SEQUENCE:
        raw, offset = _take(frame, offset, 4, "sequence")
        sequence = struct.unpack(">i", raw)[0]
    is_last = bool(flags & _LAST_NO_SEQUENCE)

    if message_type == _SERVER_FULL_RESPONSE:
        raw, offset = _take(frame, offset, 4, "payload size")
        payload_size = struct.unpack(">I", raw)[0]
        raw_payload, offset = _take(frame, offset, payload_size, "payload")
        if offset != len(frame):
            raise ValueError("unexpected bytes after Volcengine payload")
        payload = _decode_payload(serialization, compression, raw_payload)
        return ParsedFrame(
            message_type,
            flags,
            serialization,
            compression,
            sequence,
            is_last,
            payload,
        )

    if message_type == _SERVER_ERROR_RESPONSE:
        raw, offset = _take(frame, offset, 4, "error code")
        error_code = struct.unpack(">I", raw)[0]
        raw, offset = _take(frame, offset, 4, "error payload size")
        payload_size = struct.unpack(">I", raw)[0]
        raw_payload, offset = _take(frame, offset, payload_size, "error payload")
        if offset != len(frame):
            raise ValueError("unexpected bytes after Volcengine error payload")
        try:
            payload = _decode_payload(serialization, compression, raw_payload)
        except (gzip.BadGzipFile, UnicodeDecodeError, json.JSONDecodeError):
            payload = raw_payload.decode("utf-8", "replace")
        return ParsedFrame(
            message_type,
            flags,
            serialization,
            compression,
            sequence,
            True,
            payload,
            error_code,
        )

    raise ValueError(f"unexpected Volcengine server message type {message_type}")


def _extract_text(payload: Any) -> str:
    """Extract cumulative text, falling back to utterance-level results."""
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str) and text:
            return text
        utterances = result.get("utterances")
    elif isinstance(result, list):
        direct = [
            item.get("text", "")
            for item in result
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if any(direct):
            return "".join(direct)
        utterances = [
            utterance
            for item in result
            if isinstance(item, dict)
            for utterance in (item.get("utterances") or [])
        ]
    else:
        utterances = payload.get("utterances")

    if not isinstance(utterances, list):
        return ""
    return "".join(
        str(item.get("text") or "")
        for item in utterances
        if isinstance(item, dict)
    )


def _payload_error(payload: Any) -> VolcengineProtocolError | None:
    """Recognize gateways that put status information inside a normal frame."""
    if not isinstance(payload, dict):
        return None
    raw_code = payload.get("code", payload.get("status_code"))
    if raw_code in (None, 0, "0", 20000000, "20000000"):
        return None
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        code = -1
    return VolcengineProtocolError(
        code, payload.get("message", payload.get("status_text"))
    )


def _looks_like_auth_error(error: BaseException) -> bool:
    code = getattr(error, "code", None)
    if code in _AUTH_ERROR_CODES:
        return True
    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    status = status or getattr(response, "status_code", None) or getattr(
        response, "status", None
    )
    if status in (401, 403):
        return True
    detail = getattr(error, "detail", "")
    text = f"{error} {detail}".lower()
    return any(word in text for word in _AUTH_WORDS)


def _load_websockets():
    try:
        import websockets
    except Exception as error:
        raise RuntimeError(
            "websockets is not available; install the Python websockets "
            "package before recording"
        ) from error
    return websockets


def _websocket_header_kwargs(
    connect_func: Callable[..., Any], headers: dict[str, str]
) -> dict[str, Any]:
    """Support both websockets <=13 and its renamed header argument."""
    try:
        parameters = inspect.signature(connect_func).parameters
    except (TypeError, ValueError):
        parameters = {}
    key = "additional_headers" if "additional_headers" in parameters else "extra_headers"
    kwargs: dict[str, Any] = {key: headers}
    if "ping_timeout" in parameters:
        kwargs["ping_timeout"] = None
    return kwargs


def _resolve_endpoint(value: Any) -> str:
    endpoint = str(value or "bigmodel_async").strip()
    if endpoint in _ENDPOINTS:
        return _ENDPOINTS[endpoint]
    # The API key is sent in WebSocket request headers. Never allow a custom
    # plaintext ws:// endpoint to receive it.
    if endpoint.startswith("wss://"):
        return endpoint
    if endpoint.startswith("/"):
        return f"wss://openspeech.bytedance.com{endpoint}"
    raise ValueError(
        "endpoint must be bigmodel_nostream, bigmodel, bigmodel_async, "
        "or a secure wss:// URL"
    )


def _pending_audio_seconds(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = _DEFAULT_PENDING_AUDIO_SECONDS
    if not math.isfinite(seconds):
        seconds = _DEFAULT_PENDING_AUDIO_SECONDS
    return max(
        _MIN_PENDING_AUDIO_SECONDS,
        min(_MAX_PENDING_AUDIO_SECONDS, seconds),
    )


class VolcengineASRClient:
    """Thread-safe streaming ASR client backed by a private asyncio loop."""

    is_streaming = True
    # bigmodel_async marks its last server frame explicitly.  The manager must
    # wait for that frame because it can replace a live hypothesis with a more
    # accurate two-pass result immediately before finishing.
    waits_for_final_event = True

    def __init__(self, settings: dict) -> None:
        settings = settings if isinstance(settings, dict) else {}
        self._api_key = str(settings.get("api_key") or "").strip()
        self._app_id = str(
            settings.get("app_id") or settings.get("app_key") or ""
        ).strip()
        self._access_token = str(
            settings.get("access_token") or settings.get("access_key") or ""
        ).strip()
        self._resource_id = str(
            settings.get("resource_id") or DEFAULT_RESOURCE_ID
        ).strip()
        self._endpoint = _resolve_endpoint(settings.get("endpoint"))
        self._is_nostream = "bigmodel_nostream" in self._endpoint
        self._is_async = "bigmodel_async" in self._endpoint
        self.is_streaming = not self._is_nostream
        self.waits_for_final_event = not self._is_nostream
        self._language = str(settings.get("language") or "zh-CN").strip()
        self._uid = str(settings.get("uid") or "doubao-murmur-linux").strip()
        self._chunk_ms = max(100, min(1000, int(settings.get("chunk_ms") or 200)))
        self._chunk_bytes = (
            _SAMPLE_RATE
            * _CHANNELS
            * _BYTES_PER_SAMPLE
            * self._chunk_ms
            // 1000
        )
        self.max_pending_audio_seconds = _pending_audio_seconds(
            settings.get("max_pending_audio_seconds")
        )
        self.max_pending_audio_bytes = int(
            _SAMPLE_RATE
            * _CHANNELS
            * _BYTES_PER_SAMPLE
            * self.max_pending_audio_seconds
        )
        self.final_result_timeout = max(
            1.0, float(settings.get("final_result_timeout") or 20.0)
        )
        self._request_options = {
            "model_name": "bigmodel",
            "enable_itn": bool(settings.get("enable_itn", True)),
            "enable_punc": bool(settings.get("enable_punc", True)),
            "enable_ddc": bool(settings.get("enable_ddc", True)),
            "show_utterances": bool(settings.get("show_utterances", True)),
            "result_type": str(settings.get("result_type") or "full"),
        }
        boosting_table_id = str(
            settings.get("boosting_table_id") or ""
        ).strip()
        if (
            boosting_table_id
            and len(boosting_table_id) <= 256
            and not any(
                character in boosting_table_id
                for character in "\r\n\x00"
            )
        ):
            self._request_options["corpus"] = {
                "boosting_table_id": boosting_table_id
            }
        if self._is_async:
            # This is the switch that asks bigmodel_async to replace streaming
            # hypotheses with its higher-accuracy, sentence-level second pass.
            self._request_options["enable_nonstream"] = bool(
                settings.get("enable_nonstream", True)
            )
            end_window_size = int(settings.get("end_window_size") or 800)
            self._request_options["end_window_size"] = max(
                300, min(5000, end_window_size)
            )

        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task | None = None
        self._wake_event: asyncio.Event | None = None
        self._ws: Any | None = None
        self._pending_audio = bytearray()
        self._connected = False
        self._active = False
        self._finish_requested = False
        self._overflow_reported = False
        self._generation = 0
        self._last_text = ""

        # Called on the client's asyncio thread.  GTK callers marshal these
        # back through GLib.idle_add, as they do for the existing ASR clients.
        self.on_open = None
        self.on_result = None
        self.on_finish = None
        self.on_error = None
        self.on_auth_error = None

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def _has_credentials(self) -> bool:
        return bool(self._api_key or (self._app_id and self._access_token))

    def _build_headers(self, connect_id: str | None = None) -> dict[str, str]:
        """Build a fresh handshake header set without ever logging it."""
        headers = {
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Connect-Id": connect_id or str(uuid.uuid4()),
            # Required by the new API-key gateway and accepted by the legacy
            # endpoint. The request body itself uses unsequenced packets.
            "X-Api-Sequence": "-1",
        }
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        else:
            headers["X-Api-App-Key"] = self._app_id
            headers["X-Api-Access-Key"] = self._access_token
        return headers

    def _build_payload(self) -> dict[str, Any]:
        audio: dict[str, Any] = {
            "format": "pcm",
            "codec": "raw",
            "rate": _SAMPLE_RATE,
            "bits": _SAMPLE_BITS,
            "channel": _CHANNELS,
        }
        # Volcengine documents language only for the nostream endpoint.
        if self._language and self._is_nostream:
            audio["language"] = self._language
        request = dict(self._request_options)
        hotwords = load_personal_vocabulary()
        if hotwords:
            # Volcengine's request-level `context` is itself a JSON string,
            # rather than a nested object in the binary full-request JSON.
            request["context"] = json.dumps(
                {"hotwords": [{"word": term} for term in hotwords]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return {
            "user": {"uid": self._uid, "platform": "Linux"},
            "audio": audio,
            "request": request,
        }

    def connect(self, params=None) -> None:
        """Start one ASR session on a dedicated asyncio thread."""
        del params
        if not self._has_credentials():
            self._notify_error(
                RuntimeError(
                    "volcengine.json needs api_key or both app_id and access_token"
                ),
                None,
            )
            return

        # Invalidate any earlier socket before publishing this generation.
        self.disconnect()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._pending_audio.clear()
            self._connected = False
            self._active = True
            self._finish_requested = False
            self._overflow_reported = False
            self._last_text = ""
            loop = asyncio.new_event_loop()
            self._loop = loop
            thread = threading.Thread(
                target=self._run_loop,
                args=(loop, generation),
                name="volcengine-asr",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def send_audio(self, data: bytes) -> None:
        """Buffer PCM without ever waiting for a slow network sender."""
        if not data:
            return
        overflow_generation: int | None = None
        loop = None
        event = None
        with self._lock:
            if not self._active or self._finish_requested:
                return
            if self._overflow_reported:
                return
            if len(self._pending_audio) + len(data) > self.max_pending_audio_bytes:
                self._overflow_reported = True
                self._pending_audio.clear()
                overflow_generation = self._generation
            else:
                self._pending_audio.extend(data)
                loop = self._loop
                event = self._wake_event
        if overflow_generation is not None:
            self._notify_error(
                PendingAudioOverflowError(), overflow_generation
            )
            return
        if loop and event and loop.is_running():
            loop.call_soon_threadsafe(event.set)

    def finish_sending(self) -> None:
        """Flush remaining PCM as the protocol's final negative packet."""
        with self._lock:
            if not self._active or self._finish_requested:
                return
            self._finish_requested = True
            loop = self._loop
            event = self._wake_event
        if loop and event and loop.is_running():
            loop.call_soon_threadsafe(event.set)

    def disconnect(self) -> None:
        """Cancel the current generation and discard every late callback."""
        with self._lock:
            self._generation += 1
            self._active = False
            self._connected = False
            self._finish_requested = False
            self._pending_audio.clear()
            loop = self._loop
            task = self._task
            event = self._wake_event
        if loop and loop.is_running():
            if event:
                loop.call_soon_threadsafe(event.set)
            if task and not task.done():
                loop.call_soon_threadsafe(task.cancel)

    def _run_loop(
        self, loop: asyncio.AbstractEventLoop, generation: int
    ) -> None:
        asyncio.set_event_loop(loop)
        task = loop.create_task(self._connect_and_listen(generation))
        with self._lock:
            if generation == self._generation:
                self._task = task
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        finally:
            pending = asyncio.all_tasks(loop)
            for item in pending:
                item.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            with self._lock:
                if self._loop is loop:
                    self._loop = None
                    self._task = None
                    self._wake_event = None
                    self._ws = None
                    self._connected = False
                    self._active = False

    async def _connect_and_listen(self, generation: int) -> None:
        if not self._is_current(generation):
            return
        logger.info("Connecting to Volcengine ASR")
        try:
            websockets = _load_websockets()
            headers = self._build_headers()
            kwargs = _websocket_header_kwargs(websockets.connect, headers)
            async with websockets.connect(
                self._endpoint,
                open_timeout=8,
                close_timeout=3,
                max_size=2**22,
                **kwargs,
            ) as ws:
                if not self._is_current(generation):
                    return
                with self._lock:
                    self._ws = ws
                    self._connected = True
                    self._wake_event = asyncio.Event()

                await ws.send(_encode_full_request(self._build_payload()))
                logger.info("Connected to Volcengine ASR")
                self._invoke(self.on_open, generation)

                sender = asyncio.create_task(
                    self._send_audio_stream(ws, generation)
                )
                receiver = asyncio.create_task(
                    self._receive_messages(ws, generation)
                )
                try:
                    done, _ = await asyncio.wait(
                        (sender, receiver),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if receiver in done:
                        # A final response or protocol error ends the session;
                        # don't leave a sender blocked on its wake event.
                        await receiver
                        if not sender.done():
                            sender.cancel()
                    else:
                        # Sending normally completes just after the negative
                        # packet.  Keep receiving until its matching final.
                        await sender
                        await receiver
                finally:
                    for task in (sender, receiver):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(sender, receiver, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._is_current(generation):
                # Do not interpolate transport exceptions: some WebSocket
                # implementations include request headers in their repr.
                logger.error(
                    "Volcengine ASR connection failed (%s)",
                    error.__class__.__name__,
                )
                if _looks_like_auth_error(error):
                    self._notify_error(error, generation)
                else:
                    # The manager logs callback errors.  Pass it a fresh,
                    # header-free exception instead of a library exception
                    # whose text may embed the opening HTTP request.
                    self._notify_error(
                        RuntimeError(
                            "Volcengine ASR connection failed "
                            f"({error.__class__.__name__})"
                        ),
                        generation,
                    )
        finally:
            with self._lock:
                if generation == self._generation:
                    self._connected = False
                    self._ws = None

    async def _send_audio_stream(
        self,
        ws: Any,
        generation: int,
        *,
        first_sequence: int | None = None,
    ) -> None:
        sequence = first_sequence
        while self._is_current(generation):
            with self._lock:
                event = self._wake_event
                chunks: list[tuple[bytes, bool]] = []
                # Hold one packet back so an exactly aligned utterance can end
                # on audio data rather than requiring an avoidable empty frame.
                while len(self._pending_audio) > self._chunk_bytes:
                    chunk = bytes(self._pending_audio[: self._chunk_bytes])
                    del self._pending_audio[: self._chunk_bytes]
                    chunks.append((chunk, False))
                if self._finish_requested:
                    chunks.append((bytes(self._pending_audio), True))
                    self._pending_audio.clear()
                if event:
                    event.clear()

            for chunk, final in chunks:
                if not self._is_current(generation):
                    return
                await ws.send(
                    _encode_audio_request(chunk, sequence, final=final)
                )
                if final:
                    return
                if sequence is not None:
                    sequence += 1

            if not event:
                await asyncio.sleep(0)
                continue
            await event.wait()

    async def _receive_messages(self, ws: Any, generation: int) -> None:
        async for message in ws:
            if not self._is_current(generation):
                return
            if self._handle_message(message, generation):
                return
        if self._is_current(generation):
            raise ConnectionError("Volcengine ASR closed before final response")

    def _handle_message(
        self, message: Any, generation: int | None = None
    ) -> bool:
        """Dispatch one server message; return True when the session is done."""
        if generation is None:
            with self._lock:
                generation = self._generation
        if not self._is_current(generation):
            return True
        try:
            if isinstance(message, str):
                payload = json.loads(message)
                parsed = ParsedFrame(
                    _SERVER_FULL_RESPONSE,
                    _NO_SEQUENCE,
                    _SERIALIZATION_JSON,
                    _COMPRESSION_NONE,
                    None,
                    bool(payload.get("final") or payload.get("is_final")),
                    payload,
                )
            else:
                parsed = _parse_server_frame(message)
            if parsed.message_type == _SERVER_ERROR_RESPONSE:
                raise VolcengineProtocolError(
                    parsed.error_code or -1, parsed.payload
                )
            error = _payload_error(parsed.payload)
            if error:
                raise error
        except Exception as error:
            logger.error(
                "Volcengine ASR response rejected (%s)",
                error.__class__.__name__,
            )
            self._notify_error(error, generation)
            return True

        text = _extract_text(parsed.payload)
        if text:
            with self._lock:
                changed = (
                    generation == self._generation and text != self._last_text
                )
                if changed:
                    self._last_text = text
            if changed:
                self._invoke(self.on_result, generation, text)
        if parsed.is_last:
            self._invoke(self.on_finish, generation)
            return True
        return False

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return self._active and generation == self._generation

    def _invoke(
        self, callback: Callable[..., Any] | None, generation: int, *args: Any
    ) -> None:
        if not callback or not self._is_current(generation):
            return
        try:
            callback(*args)
        except Exception as error:
            logger.error("ASR callback failed (%s)", error.__class__.__name__)

    def _notify_error(
        self, error: BaseException, generation: int | None
    ) -> None:
        if generation is not None and not self._is_current(generation):
            return
        is_auth_error = _looks_like_auth_error(error)
        callback = self.on_auth_error if is_auth_error else self.on_error
        if not callback:
            return
        try:
            callback() if is_auth_error else callback(error)
        except Exception as callback_error:
            logger.error(
                "ASR callback failed (%s)", callback_error.__class__.__name__
            )
