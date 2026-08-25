"""State machine orchestrator for the recording lifecycle.

Mirrors TranscriptionManager.swift.

Key design decisions:
- GLib.idle_add() marshals callbacks from the asyncio thread to GTK main thread
- GLib.timeout_add() replaces DispatchQueue.main.asyncAfter for delayed execution
- State machine exactly mirrors macOS: idle -> starting -> recording -> stopping -> idle
"""

from __future__ import annotations

import logging
import math

from gi.repository import GLib

from doubao_murmur.app_state import AppState, LoginStatus, RecordingState
from doubao_murmur.asr_client import ASRClient
from doubao_murmur.audio_capture import AudioCapture, AudioDeviceError
from doubao_murmur.config import (
    AUTH_EXPIRY_DELAY,
    STOP_SAFETY_TIMEOUT,
    load_backend_config,
    load_volcengine_config,
)
from doubao_murmur.params_store import ASRParams, ParamsStore

logger = logging.getLogger(__name__)

_EXTERNAL_BACKENDS = frozenset({"openai", "volcengine"})
_DEFAULT_MAX_RECORDING_SECONDS = 600.0
_MIN_MAX_RECORDING_SECONDS = 1.0
_MAX_MAX_RECORDING_SECONDS = 3600.0


def _effective_backend_name(settings: dict) -> str:
    name = str(settings.get("backend", "doubao")).strip().lower()
    if name in _EXTERNAL_BACKENDS or name == "doubao":
        return name
    logger.warning("Unknown backend %r; using doubao", name)
    return "doubao"


def configured_backend_name() -> str:
    """Return the effective backend name, including the fallback policy."""
    return _effective_backend_name(load_backend_config())


def _bounded_recording_seconds(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = _DEFAULT_MAX_RECORDING_SECONDS
    if not math.isfinite(seconds):
        seconds = _DEFAULT_MAX_RECORDING_SECONDS
    return max(
        _MIN_MAX_RECORDING_SECONDS,
        min(_MAX_MAX_RECORDING_SECONDS, seconds),
    )


def _build_asr_client(settings: dict | None = None):
    """Pick the transcription backend named in backend.json."""
    if settings is None:
        settings = load_backend_config()
    name = _effective_backend_name(settings)
    if name == "openai":
        from doubao_murmur.openai_client import OpenAIASRClient

        logger.info(
            "Transcription backend: openai (%s)",
            settings.get("model") or "openai/whisper-1",
        )
        return OpenAIASRClient(settings)
    if name == "volcengine":
        from doubao_murmur.volcengine_client import VolcengineASRClient

        logger.info("Transcription backend: volcengine")
        return VolcengineASRClient(load_volcengine_config(settings))
    logger.info("Transcription backend: doubao")
    return ASRClient()


def backend_needs_doubao_login() -> bool:
    """Whether the configured backend authenticates through doubao.com."""
    return configured_backend_name() == "doubao"


class TranscriptionManager:
    """Orchestrates the recording lifecycle."""

    def __init__(self, app_state: AppState) -> None:
        self.app_state = app_state
        settings = load_backend_config()
        self.backend_name = _effective_backend_name(settings)
        self.max_recording_seconds = _bounded_recording_seconds(
            settings.get("max_recording_seconds")
        )
        self.asr_client = _build_asr_client(settings)
        self.audio_capture = AudioCapture()

        self.using_cached_params = False
        self.awaiting_final_result = False
        self.post_stop_frames = 0
        self.last_post_stop_text = ""
        self.safety_timer_id: int | None = None
        self.recording_limit_timer_id: int | None = None
        # Guards callbacks already queued on GTK's main context. ASR clients
        # also invalidate their sockets, but a callback can race with cancel
        # after it has left the worker thread and before GLib executes it.
        self._session_generation = 0

        # Callbacks set by app.py
        self.on_auth_expired = None  # () -> None
        self.on_show_login = None  # () -> None
        self.on_params_needed = None  # (callback: (ASRParams|None)->None) -> None
        self.on_overlay_show = None  # () -> None
        self.on_overlay_hide = None  # () -> None
        self.on_overlay_update = None  # (text: str) -> None
        self.on_paste = None  # (text: str) -> None
        self.on_cancel_enabled_changed = None  # (enabled: bool) -> None

        self._wire_asr_callbacks()

    def _wire_asr_callbacks(self) -> None:
        """Wire ASR client callbacks to marshal from asyncio to GTK thread."""
        source_client = self.asr_client
        source_client.on_open = lambda: self._marshal_asr_callback(
            source_client, self._on_asr_open
        )
        source_client.on_result = lambda text: self._marshal_asr_callback(
            source_client, self._on_asr_result, text
        )
        source_client.on_finish = lambda: self._marshal_asr_callback(
            source_client, self._on_asr_finish
        )
        source_client.on_error = lambda err: self._marshal_asr_callback(
            source_client, self._on_asr_error, err
        )
        source_client.on_auth_error = lambda: self._marshal_asr_callback(
            source_client, self._on_auth_error
        )

    def _marshal_asr_callback(self, source_client, callback, *args) -> int:
        """Queue a callback only for the currently installed ASR client."""
        if source_client is not self.asr_client:
            return GLib.SOURCE_REMOVE
        generation = self._session_generation
        return GLib.idle_add(
            self._run_asr_callback_if_current,
            generation,
            source_client,
            callback,
            *args,
        )

    def _run_asr_callback_if_current(
        self, generation: int, source_client, callback, *args
    ) -> bool:
        if (
            generation != self._session_generation
            or source_client is not self.asr_client
        ):
            return GLib.SOURCE_REMOVE
        return callback(*args)

    def reload_backend(self) -> bool:
        """Reload backend settings without disrupting an active recording."""
        if self.app_state.recording_state != RecordingState.IDLE:
            return False

        try:
            settings = load_backend_config()
            backend_name = _effective_backend_name(settings)
            max_recording_seconds = _bounded_recording_seconds(
                settings.get("max_recording_seconds")
            )
            new_client = _build_asr_client(settings)
        except Exception as error:
            # Client construction can surface credential-bearing exceptions.
            # Log only the type, never the submitted key or exception text.
            logger.error("Backend reload failed (%s)", error.__class__.__name__)
            return False

        old_client = self.asr_client
        # Swap first: even if the old worker calls a saved callback during
        # teardown, source identity makes it stale immediately. Incrementing
        # the generation also invalidates callbacks already queued in GLib.
        self.asr_client = new_client
        self._session_generation += 1
        self.backend_name = backend_name
        self.max_recording_seconds = max_recording_seconds
        self._clear_recording_limit_timer()
        self._clear_safety_timer()
        self.using_cached_params = False
        self.awaiting_final_result = False
        self.post_stop_frames = 0
        self.last_post_stop_text = ""
        self._wire_asr_callbacks()

        for callback_name in (
            "on_open",
            "on_result",
            "on_finish",
            "on_error",
            "on_auth_error",
        ):
            try:
                setattr(old_client, callback_name, None)
            except Exception:
                pass
        try:
            old_client.disconnect()
        except Exception as error:
            logger.warning(
                "Previous backend teardown failed (%s)",
                error.__class__.__name__,
            )
        return True

    # --- Toggle ---

    def handle_toggle(self) -> None:
        """Called on GTK main thread from hotkey manager."""
        state = self.app_state.recording_state
        if state == RecordingState.IDLE:
            self._start_recording()
        elif state in (RecordingState.STARTING, RecordingState.RECORDING):
            self._stop_recording()
        # STOPPING: ignore

    def _start_recording(self) -> None:
        if (
            self.backend_name == "doubao"
            and self.app_state.login_status != LoginStatus.LOGGED_IN
        ):
            logger.warning("Not logged in, showing login window")
            if self.on_show_login:
                self.on_show_login()
            return

        self._clear_recording_limit_timer()
        self._session_generation += 1
        logger.info("Starting recording...")
        self._harvest_clipboard()
        self._set_state(RecordingState.STARTING)
        self.app_state.transcription_text = ""
        self.app_state.error_message = None
        if self.on_overlay_show:
            self.on_overlay_show()

        # Start audio immediately (buffered in ASR client until WS connects)
        try:
            self.audio_capture.start(on_audio_data=self.asr_client.send_audio)
        except AudioDeviceError as error:
            # AudioDeviceError messages are deliberately bounded and contain
            # no captured audio or provider credentials, so the user can see
            # why automatic routing refused to guess.
            logger.error("Audio capture failed: %s", error)
            self.app_state.error_message = f"麦克风不可用：{error}"
            GLib.timeout_add(int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle)
            return
        except Exception as error:
            logger.error("Audio capture failed (%s)", error.__class__.__name__)
            self.app_state.error_message = "麦克风启动失败"
            GLib.timeout_add(int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle)
            return

        self._arm_recording_limit_timer(self._session_generation)

        # Backends that carry their own credentials need no doubao params.
        if self.backend_name != "doubao":
            self.using_cached_params = False
            self.asr_client.connect(None)
            return

        # Try cached params first, fall back to WebView extraction
        cached = ParamsStore.load()
        if cached:
            logger.info("Using cached ASR params")
            self.using_cached_params = True
            self.asr_client.connect(cached)
        elif self.on_params_needed:
            self.using_cached_params = False
            self.on_params_needed(self._on_params_extracted)
        else:
            self.app_state.error_message = "无法获取连接参数，请重新登录"
            GLib.timeout_add(int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle)

    def _harvest_clipboard(self) -> None:
        """Fold whatever is on the clipboard into the vocabulary hints.

        Sampled here rather than on a timer so the app only ever reads the
        clipboard in response to the user starting a dictation, and reads
        it early enough that the request built at stop time already has
        the updated terms.
        """
        settings = load_backend_config()
        options = settings.get("auto_glossary") or {}
        if not options.get("enabled"):
            return
        try:
            from gi.repository import Gdk

            from doubao_murmur.glossary import shared_glossary

            display = Gdk.Display.get_default()
            if display is None:
                return
            glossary = shared_glossary(int(options.get("max_terms") or 48))

            def on_text(clipboard, result):
                try:
                    text = clipboard.read_text_finish(result)
                except Exception:
                    return
                added = glossary.harvest(text or "")
                if added:
                    logger.info("Glossary: %d term(s) from clipboard", added)

            display.get_clipboard().read_text_async(None, on_text)
        except Exception as error:
            logger.warning(
                "Clipboard harvest skipped (%s)", error.__class__.__name__
            )

    def _stop_recording(self) -> None:
        logger.info("Stopping recording...")
        self._clear_recording_limit_timer()
        self._set_state(RecordingState.STOPPING)
        self.audio_capture.stop()
        self.asr_client.finish_sending()
        self.awaiting_final_result = True
        self.post_stop_frames = 0
        self.last_post_stop_text = self.app_state.transcription_text

        # Different streaming services have different finalization latency.
        # The client advertises its own backstop; legacy clients retain the
        # original Doubao timeout.
        try:
            final_timeout = float(
                getattr(
                    self.asr_client,
                    "final_result_timeout",
                    STOP_SAFETY_TIMEOUT,
                )
            )
            if final_timeout <= 0:
                raise ValueError("timeout must be positive")
        except (TypeError, ValueError):
            final_timeout = STOP_SAFETY_TIMEOUT

        # Safety timeout
        self.safety_timer_id = GLib.timeout_add(
            max(1, int(final_timeout * 1000)),
            self._safety_timeout,
            self._session_generation,
        )

    def _arm_recording_limit_timer(self, generation: int) -> None:
        self._clear_recording_limit_timer()
        self.recording_limit_timer_id = GLib.timeout_add(
            max(1, int(self.max_recording_seconds * 1000)),
            self._recording_limit_timeout,
            generation,
        )

    def _clear_recording_limit_timer(self) -> None:
        timer_id = getattr(self, "recording_limit_timer_id", None)
        if timer_id is not None:
            GLib.source_remove(timer_id)
            self.recording_limit_timer_id = None

    def _clear_safety_timer(self) -> None:
        if self.safety_timer_id is not None:
            GLib.source_remove(self.safety_timer_id)
            self.safety_timer_id = None

    def _recording_limit_timeout(self, generation: int) -> bool:
        if generation != self._session_generation:
            return GLib.SOURCE_REMOVE
        self.recording_limit_timer_id = None
        if self.app_state.recording_state in (
            RecordingState.STARTING,
            RecordingState.RECORDING,
        ):
            logger.info(
                "Recording duration limit reached; finalizing normally"
            )
            self._stop_recording()
        return GLib.SOURCE_REMOVE

    def _safety_timeout(self, generation: int | None = None) -> bool:
        if (
            generation is not None
            and generation != self._session_generation
        ):
            return GLib.SOURCE_REMOVE
        # The source is executing now; clear the id before completion/reset so
        # reset doesn't try to remove the currently running GLib callback.
        self.safety_timer_id = None
        if self.app_state.recording_state == RecordingState.STOPPING:
            logger.info("Safety timeout, completing with current text")
            self.awaiting_final_result = False
            self._complete_transcription()
        return GLib.SOURCE_REMOVE

    # --- ASR callbacks (on GTK main thread via GLib.idle_add) ---

    def _on_asr_open(self) -> bool:
        if self.app_state.recording_state == RecordingState.STARTING:
            self._set_state(RecordingState.RECORDING)
        return GLib.SOURCE_REMOVE

    def _on_asr_result(self, text: str) -> bool:
        self.app_state.transcription_text = text
        if self.on_overlay_update:
            self.on_overlay_update(text)
        if self.app_state.recording_state == RecordingState.STARTING:
            self._set_state(RecordingState.RECORDING)

        if not self.awaiting_final_result:
            return GLib.SOURCE_REMOVE

        # A batch backend sends exactly one result, which is already final;
        # waiting for a second frame to match would just burn the safety
        # timeout before pasting.
        if not getattr(self.asr_client, "is_streaming", True):
            self.awaiting_final_result = False
            self._clear_safety_timer()
            self._complete_transcription()
            return GLib.SOURCE_REMOVE

        # Optimized bidirectional clients send an explicit terminal frame only
        # after the non-streaming second pass. Never paste a live hypothesis,
        # even if two adjacent frames happen to contain identical text.
        if getattr(self.asr_client, "waits_for_final_event", False):
            return GLib.SOURCE_REMOVE

        # Results are cumulative rewrites and keep being corrected after
        # the user stops, so completing on the first post-stop frame drops
        # the final pass. Wait until two consecutive frames agree; the
        # safety timeout is the backstop. (Waiting for an "finish" event
        # is not an option -- the server does not send one.)
        if self.post_stop_frames and text == self.last_post_stop_text:
            self.awaiting_final_result = False
            self._clear_safety_timer()
            self._complete_transcription()
            return GLib.SOURCE_REMOVE

        self.post_stop_frames += 1
        self.last_post_stop_text = text
        return GLib.SOURCE_REMOVE

    def _on_asr_finish(self) -> bool:
        should_complete = self.awaiting_final_result or (
            self.app_state.recording_state == RecordingState.RECORDING
        )
        self.awaiting_final_result = False
        self._clear_safety_timer()
        if should_complete and self.app_state.recording_state in (
            RecordingState.STOPPING,
            RecordingState.RECORDING,
        ):
            self._complete_transcription()
        return GLib.SOURCE_REMOVE

    def _on_asr_error(self, error) -> bool:
        if self.app_state.recording_state == RecordingState.IDLE:
            return GLib.SOURCE_REMOVE
        # Transport faults -- handshake timeouts, dropped sockets, missed
        # pongs -- are not auth failures, and clearing the saved params for
        # them forced a full re-login after every network hiccup. Real auth
        # failures arrive separately via ASRClient.on_auth_error.
        logger.error(
            "ASR transport error (credentials kept; %s)",
            error.__class__.__name__,
        )
        self.awaiting_final_result = False
        self._clear_recording_limit_timer()
        self._clear_safety_timer()
        self.audio_capture.stop()
        self.asr_client.disconnect()
        self._set_state(RecordingState.STOPPING)
        self.app_state.error_message = "连接出错"
        GLib.timeout_add(int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle)
        return GLib.SOURCE_REMOVE

    def _on_auth_error(self) -> bool:
        self._handle_auth_failure()
        return GLib.SOURCE_REMOVE

    # --- Completion & Reset ---

    def _complete_transcription(self) -> None:
        text = self.app_state.transcription_text.strip()
        # Dictation can contain private messages, credentials or unpublished
        # text. Keep useful diagnostics without persisting any of that content.
        logger.info("Completing transcription (%d characters)", len(text))
        if text and self.on_paste:
            self.on_paste(text)
        self._reset_to_idle()

    def _reset_to_idle(self) -> bool:
        self._session_generation += 1
        reset_generation = self._session_generation
        self.awaiting_final_result = False
        self._clear_recording_limit_timer()
        self._clear_safety_timer()
        self.audio_capture.stop()
        self.asr_client.disconnect()
        self._set_state(RecordingState.IDLE)
        self.app_state.error_message = None
        if self.on_overlay_hide:
            self.on_overlay_hide()
        self.using_cached_params = False
        # Clear text after short delay
        GLib.timeout_add(200, self._clear_transcription_if_current, reset_generation)
        return GLib.SOURCE_REMOVE

    def _clear_transcription_if_current(self, generation: int) -> bool:
        if generation == self._session_generation:
            self.app_state.transcription_text = ""
        return GLib.SOURCE_REMOVE

    def handle_cancel(self) -> None:
        if self.app_state.recording_state == RecordingState.IDLE:
            return
        logger.info("Cancelling transcription")
        self.awaiting_final_result = False
        self.audio_capture.stop()
        self.asr_client.disconnect()
        self._reset_to_idle()

    def _handle_auth_failure(self) -> None:
        if self.backend_name != "doubao":
            # External backends carry their own credentials.  A rejected API
            # key must not destroy a perfectly valid cached doubao.com login
            # or surface the unrelated WebView re-login dialog.
            logger.warning("External backend credentials were rejected")
            self.awaiting_final_result = False
            self._clear_recording_limit_timer()
            self._clear_safety_timer()
            self.using_cached_params = False
            self.audio_capture.stop()
            self.asr_client.disconnect()
            self.app_state.error_message = "语音服务认证失败，请检查 API 凭证"
            GLib.timeout_add(int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle)
            return

        logger.warning("Auth failure, clearing cached params")
        ParamsStore.clear()
        self.using_cached_params = False
        self.audio_capture.stop()
        self.asr_client.disconnect()
        self._reset_to_idle()
        self.app_state.login_status = LoginStatus.NOT_LOGGED_IN
        if self.on_auth_expired:
            self.on_auth_expired()

    def _set_state(self, new_state: RecordingState) -> None:
        self.app_state.recording_state = new_state
        if self.on_cancel_enabled_changed:
            self.on_cancel_enabled_changed(new_state != RecordingState.IDLE)

    def _on_params_extracted(self, params: ASRParams | None) -> None:
        """Called when WebView param extraction completes."""
        if params:
            ParamsStore.save(params)
            self.asr_client.connect(params)
        else:
            self.app_state.error_message = "无法获取连接参数，请重新登录"
            GLib.timeout_add(int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle)
