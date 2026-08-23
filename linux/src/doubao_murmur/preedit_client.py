"""Deliver streaming transcription through the temporary Murmur IBus engine.

The client deliberately owns only routing state.  It does not inspect or log
transcription text, and it leaves the existing clipboard/paste path to its
caller when :meth:`PreeditClient.acquire` returns ``False``.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from enum import Enum
from typing import Any

from gi.repository import Gio, GLib

from doubao_murmur.host_tools import command_candidates

logger = logging.getLogger(__name__)

PREEDIT_BUS_NAME = "org.murmur.IME.Preedit1"
PREEDIT_OBJECT_PATH = "/org/murmur/IME/Preedit1"
PREEDIT_INTERFACE = "org.murmur.IME.Preedit1"
PREEDIT_ENGINE = "murmur-voice"

_DBUS_TIMEOUT_MS = 250
_IBUS_TIMEOUT_SECONDS = 3
_ENGINE_SWITCH_VERIFY_SECONDS = 1.0
_MAX_UINT64 = (1 << 64) - 1
_MAX_UTTERANCE_ID_BYTES = 256


class AcquireResult(Enum):
    """Why an inline-preedit acquisition did or did not start.

    Only ``UNAVAILABLE`` is safe for the legacy clipboard/paste fallback.
    ``REJECTED`` means the IBus service was reached but the current input
    context refused voice input (for example a password or private field).
    """

    ACQUIRED = "acquired"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


def _default_proxy_factory() -> Gio.DBusProxy:
    """Create a non-activating proxy for the currently running IBus engine."""
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION,
        Gio.DBusProxyFlags.DO_NOT_AUTO_START,
        None,
        PREEDIT_BUS_NAME,
        PREEDIT_OBJECT_PATH,
        PREEDIT_INTERFACE,
        None,
    )


class PreeditClient:
    """Switch to ``murmur-voice`` and route one strict preedit utterance.

    One instance keeps one ``Gio.DBusProxy`` for its lifetime.  Consequently
    all calls in an utterance have the same D-Bus unique sender, which the IBus
    engine uses together with the utterance id to reject unrelated callers.

    Callers that need a paste fallback must use ``acquire_result()`` and accept
    only ``UNAVAILABLE``. Once acquisition succeeds, a failed partial/final
    must not be converted into a paste because the engine may already have
    displayed or committed text.
    """

    def __init__(
        self,
        *,
        proxy_factory: Callable[[], Any] | None = None,
        command_provider: Callable[[str], list[list[str]]] | None = None,
        command_runner: Callable[..., Any] | None = None,
        dbus_timeout_ms: int = _DBUS_TIMEOUT_MS,
        acquire_retry_seconds: float = 1.0,
        acquire_retry_interval: float = 0.05,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._proxy_factory = proxy_factory or _default_proxy_factory
        self._command_provider = command_provider or command_candidates
        self._command_runner = command_runner or subprocess.run
        self._dbus_timeout_ms = max(1, int(dbus_timeout_ms))
        self._acquire_retry_seconds = max(0.0, float(acquire_retry_seconds))
        self._acquire_retry_interval = max(0.001, float(acquire_retry_interval))
        self._monotonic = monotonic or time.monotonic
        self._sleeper = sleeper or time.sleep

        self._proxy: Any | None = None
        self._utterance_id: str | None = None
        self._last_revision = 0
        self._original_engine: str | None = None
        self._switched_engine = False
        self._pending_restore_engine: str | None = None
        self._lock = threading.RLock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._utterance_id is not None

    @property
    def utterance_id(self) -> str | None:
        with self._lock:
            return self._utterance_id

    @property
    def last_revision(self) -> int:
        with self._lock:
            return self._last_revision

    @property
    def original_engine(self) -> str | None:
        with self._lock:
            return self._original_engine

    @property
    def restore_pending(self) -> bool:
        with self._lock:
            return self._pending_restore_engine is not None

    def current_engine(self) -> str | None:
        """Return the host IBus engine name, including from inside Flatpak."""
        for prefix in self._ibus_command_candidates():
            try:
                result = self._command_runner(
                    prefix + ["engine"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=_IBUS_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            engine = self._parse_engine_name(getattr(result, "stdout", ""))
            if engine is not None:
                return engine
        logger.warning("Could not determine the current IBus engine")
        return None

    def acquire(self, utterance_id: str) -> bool:
        """Compatibility wrapper returning whether inline preedit was acquired."""
        return self.acquire_result(utterance_id) is AcquireResult.ACQUIRED

    def acquire_result(self, utterance_id: str) -> AcquireResult:
        """Acquire inline preedit and preserve the reason for a safe fallback.

        Switching engines briefly creates an IBus command-line ``fake`` focus
        context.  The real application regains focus immediately afterwards,
        so an explicit rejection is retried for a short bounded interval.
        """
        if not self._valid_utterance_id(utterance_id):
            return AcquireResult.REJECTED

        with self._lock:
            if self._utterance_id is not None:
                return AcquireResult.REJECTED
            if not self._retry_pending_restore():
                return AcquireResult.REJECTED

            original_engine = self.current_engine()
            if original_engine is None:
                return AcquireResult.UNAVAILABLE

            self._original_engine = original_engine
            self._switched_engine = original_engine != PREEDIT_ENGINE
            if self._switched_engine and not self._set_engine(PREEDIT_ENGINE):
                # Some IBus versions change the engine but report a failure
                # exit status. Verification below normally catches that; this
                # is a final best-effort guard for an unreadable host state.
                self._restore_original_engine()
                self._clear_session_state()
                return AcquireResult.UNAVAILABLE

            accepted = False
            result = AcquireResult.REJECTED
            saw_explicit_rejection = False
            saw_call_failure = False
            try:
                deadline = self._monotonic() + self._acquire_retry_seconds
                while True:
                    response = self._call_optional_bool(
                        "Acquire",
                        GLib.Variant("(s)", (utterance_id,)),
                        log_failure=False,
                    )
                    if response is None:
                        saw_call_failure = True
                    elif response is False:
                        saw_explicit_rejection = True
                    if response:
                        accepted = True
                        result = AcquireResult.ACQUIRED
                        break
                    if self._monotonic() >= deadline:
                        if saw_explicit_rejection:
                            result = AcquireResult.REJECTED
                        elif self._proxy_has_owner() is False:
                            result = AcquireResult.UNAVAILABLE
                        else:
                            # A timeout or malformed response from a reachable
                            # or unknown service is unsafe for paste fallback.
                            result = AcquireResult.REJECTED
                        break
                    self._sleeper(self._acquire_retry_interval)
            finally:
                if not accepted:
                    if saw_call_failure:
                        logger.warning("Murmur preedit Acquire call failed")
                    self._restore_original_engine()
                    self._clear_session_state()

            if not accepted:
                return result

            self._utterance_id = utterance_id
            self._last_revision = 0
            return AcquireResult.ACQUIRED

    def partial(self, utterance_id: str, revision: int, text: str) -> bool:
        """Replace the current preedit with a strictly newer hypothesis."""
        with self._lock:
            if not self._valid_event(utterance_id, revision, text):
                return False
            accepted = self._call_bool(
                "Partial",
                GLib.Variant("(sts)", (utterance_id, revision, text)),
            )
            if accepted:
                self._last_revision = revision
            return accepted

    def final(self, utterance_id: str, revision: int, text: str) -> bool:
        """Commit one newer final result, then restore the previous engine."""
        with self._lock:
            if not self._valid_event(utterance_id, revision, text):
                return False
            try:
                accepted = self._call_bool(
                    "Final",
                    GLib.Variant("(sts)", (utterance_id, revision, text)),
                )
                if accepted:
                    self._last_revision = revision
                return accepted
            finally:
                self._restore_original_engine()
                self._clear_session_state()

    def cancel(self, utterance_id: str) -> bool:
        """Clear an acquired preedit, then restore the previous IBus engine."""
        with self._lock:
            if utterance_id != self._utterance_id:
                return False
            try:
                return self._call_bool("Cancel", GLib.Variant("(s)", (utterance_id,)))
            finally:
                self._restore_original_engine()
                self._clear_session_state()

    def close(self) -> None:
        """Best-effort cleanup for application shutdown."""
        with self._lock:
            utterance_id = self._utterance_id
            if utterance_id is not None:
                self.cancel(utterance_id)
            self._retry_pending_restore()

    def _valid_event(self, utterance_id: str, revision: int, text: str) -> bool:
        return (
            utterance_id == self._utterance_id
            and isinstance(revision, int)
            and not isinstance(revision, bool)
            and self._last_revision < revision <= _MAX_UINT64
            and isinstance(text, str)
        )

    def _call_bool(self, method: str, parameters: GLib.Variant) -> bool:
        return self._call_optional_bool(method, parameters) is True

    def _call_optional_bool(
        self,
        method: str,
        parameters: GLib.Variant,
        *,
        log_failure: bool = True,
    ) -> bool | None:
        try:
            if self._proxy is None:
                self._proxy = self._proxy_factory()
            result = self._proxy.call_sync(
                method,
                parameters,
                Gio.DBusCallFlags.NO_AUTO_START,
                self._dbus_timeout_ms,
                None,
            )
            unpacked = result.unpack()
        except Exception:
            # Never include exception text here.  A remote error could reflect
            # method parameters, which for Partial/Final contain dictated text.
            if log_failure:
                logger.warning("Murmur preedit D-Bus call failed (%s)", method)
            return None
        valid_result = (
            isinstance(unpacked, tuple)
            and len(unpacked) == 1
            and type(unpacked[0]) is bool
        )
        if not valid_result:
            return None
        return unpacked[0]

    def _proxy_has_owner(self) -> bool | None:
        try:
            if self._proxy is None:
                self._proxy = self._proxy_factory()
            getter = getattr(self._proxy, "get_name_owner", None)
            if getter is None:
                return None
            return bool(getter())
        except Exception:
            return None

    def _restore_original_engine(self) -> bool:
        original_engine = self._original_engine or self._pending_restore_engine
        if not self._switched_engine or original_engine is None:
            return True
        restored = self._set_engine(original_engine)
        if not restored:
            self._pending_restore_engine = original_engine
            logger.warning("Could not restore the previous IBus engine")
        else:
            self._pending_restore_engine = None
        return restored

    def _retry_pending_restore(self) -> bool:
        engine = self._pending_restore_engine
        if engine is None:
            return True
        if not self._set_engine(engine):
            return False
        self._pending_restore_engine = None
        return True

    def _set_engine(self, engine: str) -> bool:
        for prefix in self._ibus_command_candidates():
            try:
                self._command_runner(
                    prefix + ["engine", engine],
                    # Ubuntu's IBus 1.5 CLI can return 1 after successfully
                    # changing the engine. Treat the observable engine state,
                    # not this unreliable status, as authoritative.
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_IBUS_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            deadline = self._monotonic() + _ENGINE_SWITCH_VERIFY_SECONDS
            while True:
                if self.current_engine() == engine:
                    return True
                if self._monotonic() >= deadline:
                    break
                self._sleeper(self._acquire_retry_interval)
        logger.warning("Could not switch the IBus engine")
        return False

    def _ibus_command_candidates(self) -> list[list[str]]:
        """Use one IBus session consistently, preferring the Flatpak host."""
        commands = [list(command) for command in self._command_provider("ibus")]
        host_commands = [
            command
            for command in commands
            if command[:2] == ["flatpak-spawn", "--host"]
        ]
        return host_commands or commands

    def _clear_session_state(self) -> None:
        self._utterance_id = None
        self._last_revision = 0
        self._original_engine = None
        self._switched_engine = False

    @staticmethod
    def _valid_utterance_id(utterance_id: str) -> bool:
        if not isinstance(utterance_id, str) or not utterance_id.strip():
            return False
        if "\x00" in utterance_id:
            return False
        return len(utterance_id.encode("utf-8")) <= _MAX_UTTERANCE_ID_BYTES

    @staticmethod
    def _parse_engine_name(output: Any) -> str | None:
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        if not isinstance(output, str):
            return None
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if len(lines) != 1:
            return None
        engine = lines[0]
        if len(engine) > 256 or any(char.isspace() for char in engine):
            return None
        return engine


__all__: Sequence[str] = (
    "PREEDIT_BUS_NAME",
    "PREEDIT_ENGINE",
    "PREEDIT_INTERFACE",
    "PREEDIT_OBJECT_PATH",
    "AcquireResult",
    "PreeditClient",
)
