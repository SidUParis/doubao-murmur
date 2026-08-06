"""Batch transcription against an OpenAI-compatible endpoint.

Presents the same surface as ASRClient (connect / send_audio /
finish_sending / disconnect plus the on_* callbacks) so
TranscriptionManager can drive either one.

Unlike the doubao WebSocket, this is a batch backend: audio is buffered
while the user speaks and uploaded once, so no partial text appears
during dictation. `is_streaming` is False, which tells the manager to
finish on the single result instead of waiting for two frames to agree.

In exchange there is no handshake to fail before recording starts, and
the `prompt` field biases recognition toward vocabulary the model would
otherwise mangle (measured: xdotool, flatpak, localStorage, AltGr and
Codex all recovered, exactly the class of error doubao got wrong).

Latency here is dominated by upload, not by the model, so `_encode`
matters more than the model choice -- see its docstring.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import struct
import subprocess
import threading
import urllib.error
import urllib.request
import uuid

from doubao_murmur.config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    BACKEND_REQUEST_TIMEOUT,
)

logger = logging.getLogger(__name__)

_SAMPLE_WIDTH = 2  # AUDIO_DTYPE is int16


def _wav_container(pcm: bytes) -> bytes:
    """Wrap raw little-endian int16 PCM in a WAV header.

    AudioCapture hands us headerless frames; the HTTP endpoint needs a
    container it can identify.
    """
    byte_rate = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * _SAMPLE_WIDTH
    block_align = AUDIO_CHANNELS * _SAMPLE_WIDTH
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, AUDIO_CHANNELS, AUDIO_SAMPLE_RATE,
        byte_rate, block_align, _SAMPLE_WIDTH * 8,
    )
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def _encode(pcm: bytes, bitrate_k: int) -> tuple[bytes, str]:
    """Compress PCM to Opus, or hand back a WAV if that is not possible.

    Upload bandwidth, not model time, dominates this request: the same
    30 s clip took 23.8 s as a 938 KB WAV and 3.6 s at 45 KB, tracking
    size almost exactly (~40 KB/s observed). Speech at 12-24 kbps
    transcribed identically in testing, so the compression is close to
    free -- it just has to happen before the upload.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(AUDIO_SAMPLE_RATE),
                "-ac", str(AUDIO_CHANNELS), "-i", "pipe:0",
                "-c:a", "libopus", "-b:a", f"{bitrate_k}k",
                "-application", "voip", "-f", "ogg", "pipe:1",
            ],
            input=pcm, capture_output=True, timeout=30, check=True,
        )
        if proc.stdout:
            return proc.stdout, "dictation.ogg"
        raise RuntimeError("encoder produced no output")
    except Exception as e:
        logger.warning(
            "Opus encoding unavailable (%s); uploading WAV, which is "
            "roughly %dx larger and that much slower to send",
            e, max(1, 256 // max(bitrate_k, 1)),
        )
        return _wav_container(pcm), "dictation.wav"


def _multipart(fields: dict[str, str], filename: str, blob: bytes) -> tuple:
    """Build a multipart/form-data body. Returns (content_type, body)."""
    boundary = uuid.uuid4().hex
    sep = f"--{boundary}\r\n".encode()
    out = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        out += sep
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode() + b"\r\n"
    out += sep
    out += (
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\n'
    ).encode()
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    out += f"Content-Type: {ctype}\r\n\r\n".encode()
    out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(out)


class OpenAIASRClient:
    """Buffer dictation audio, then transcribe it in one request."""

    #: Batch backend: the manager completes on the first (only) result.
    is_streaming = False

    def __init__(self, settings: dict) -> None:
        self._base_url = str(settings.get("base_url", "")).rstrip("/")
        self._api_key = str(settings.get("api_key", ""))
        self._model = str(settings.get("model") or "openai/whisper-1")
        self._prompt = settings.get("prompt") or None
        self._language = settings.get("language") or None
        self._bitrate_k = int(settings.get("upload_bitrate_kbps") or 16)
        glossary_opts = settings.get("auto_glossary") or {}
        self._auto_glossary = bool(glossary_opts.get("enabled"))
        self._glossary_max = int(glossary_opts.get("max_terms") or 48)

        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._active = False
        self._generation = 0  # invalidates in-flight replies after a cancel

        self.on_open = None  # () -> None
        self.on_result = None  # (text: str) -> None
        self.on_finish = None  # () -> None
        self.on_error = None  # (error: Exception | None) -> None
        self.on_auth_error = None  # () -> None

    @property
    def is_connected(self) -> bool:
        return self._active

    def connect(self, params=None) -> None:
        """Begin an utterance.

        Nothing is sent yet, so this only opens the local buffer. Firing
        on_open immediately moves the UI out of STARTING, which is
        honest here: there is no handshake that could fail later.
        """
        if not self._base_url or not self._api_key:
            self._fail(
                RuntimeError(
                    "backend.json is missing base_url or api_key"
                )
            )
            return
        with self._lock:
            self._chunks = []
            self._active = True
            self._generation += 1
        if self.on_open:
            self.on_open()

    def send_audio(self, data: bytes) -> None:
        """Buffer PCM. Called from the audio capture thread."""
        with self._lock:
            if self._active:
                self._chunks.append(data)

    def finish_sending(self) -> None:
        """Upload what was captured and report the transcript."""
        with self._lock:
            if not self._active:
                return
            pcm = b"".join(self._chunks)
            self._chunks = []
            self._active = False
            generation = self._generation

        seconds = len(pcm) / (AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * _SAMPLE_WIDTH)
        if not pcm:
            logger.info("Nothing captured; skipping transcription")
            if self.on_finish:
                self.on_finish()
            return

        logger.info("Transcribing %.1fs of audio via %s", seconds, self._model)
        threading.Thread(
            target=self._transcribe, args=(pcm, generation), daemon=True
        ).start()

    def disconnect(self) -> None:
        """Drop buffered audio and ignore any reply still in flight."""
        with self._lock:
            self._chunks = []
            self._active = False
            self._generation += 1

    # -- worker -------------------------------------------------------------

    def _prompt_for_request(self) -> str | None:
        """Hand-written prompt, plus clipboard-learned terms if enabled."""
        if not self._auto_glossary:
            return self._prompt
        try:
            from doubao_murmur.glossary import shared_glossary

            return shared_glossary(self._glossary_max).build_prompt(self._prompt)
        except Exception as e:
            logger.warning("Glossary unavailable (%s); using base prompt", e)
            return self._prompt

    def _transcribe(self, pcm: bytes, generation: int) -> None:
        fields = {
            "model": self._model,
            "prompt": self._prompt_for_request(),
            "language": self._language,
            "response_format": "json",
        }
        blob, filename = _encode(pcm, self._bitrate_k)
        logger.info(
            "Uploading %s (%.0f KB)", filename, len(blob) / 1024
        )
        content_type, body = _multipart(fields, filename, blob)
        request = urllib.request.Request(
            f"{self._base_url}/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": content_type,
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=BACKEND_REQUEST_TIMEOUT
            ) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            text = (payload.get("text") or "").strip()
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            if e.code in (401, 403):
                logger.error("Transcription rejected the credentials: %s", detail)
                if self._still_current(generation) and self.on_auth_error:
                    self.on_auth_error()
                return
            self._fail(RuntimeError(f"HTTP {e.code}: {detail}"), generation)
            return
        except Exception as e:
            self._fail(e, generation)
            return

        if not self._still_current(generation):
            logger.info("Transcript arrived after cancel; discarding")
            return

        logger.info("Transcript: %d chars", len(text))
        if text and self.on_result:
            self.on_result(text)
        if self.on_finish:
            self.on_finish()

    def _still_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _fail(self, error: Exception, generation: int | None = None) -> None:
        logger.error("Transcription failed: %s", error)
        if generation is not None and not self._still_current(generation):
            return
        if self.on_error:
            self.on_error(error)
