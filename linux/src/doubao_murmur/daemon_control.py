"""Small client for the standalone Open Voice Input daemon.

The compatibility Flatpak is deliberately only a controller.  It never reads
the daemon configuration and it has no microphone or provider implementation;
the only shared boundary is the private Unix socket below ``XDG_RUNTIME_DIR``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

MAX_RESPONSE_BYTES = 4096
CONTROL_TIMEOUT_SECONDS = 50.0
COMMANDS = frozenset({"start", "stop", "toggle", "cancel", "status"})
STATES = frozenset({"idle", "starting", "recording", "stopping", "observing"})
_CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")

_SAFE_ERRORS = {
    "invalid-command": "unsupported daemon command",
    "runtime-unavailable": "private runtime directory is unavailable",
    "socket-unavailable": "voice daemon is unavailable",
    "request-timeout": "voice daemon did not answer in time",
    "invalid-response": "voice daemon returned an invalid response",
    "response-too-large": "voice daemon response exceeded its limit",
}


class DaemonControlError(RuntimeError):
    """A bounded error that never includes socket data or configuration."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_ERRORS else "invalid-response"
        super().__init__(_SAFE_ERRORS[self.code])


@dataclass(frozen=True, slots=True)
class DaemonReply:
    ok: bool
    code: str
    state: str


class DaemonController:
    """Send one allowlisted command to a private same-user daemon socket."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str] | None = None,
        *,
        timeout: float = CONTROL_TIMEOUT_SECONDS,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._requested_path = Path(socket_path) if socket_path is not None else None
        self._timeout = max(0.05, min(CONTROL_TIMEOUT_SECONDS, float(timeout)))
        self._environ = environ if environ is not None else os.environ

    def request(self, command: str) -> DaemonReply:
        if command not in COMMANDS:
            raise DaemonControlError("invalid-command")
        path = self._socket_path()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + self._timeout
        try:
            self._set_remaining_timeout(client, deadline)
            client.connect(str(path))
            self._set_remaining_timeout(client, deadline)
            client.sendall((command + "\n").encode("ascii"))
            response = self._receive_response(client, deadline)
        except (TimeoutError, socket.timeout) as error:
            raise DaemonControlError("request-timeout") from error
        except OSError as error:
            raise DaemonControlError("socket-unavailable") from error
        finally:
            client.close()
        return self._parse_response(response)

    def _socket_path(self) -> Path:
        runtime_value = self._environ.get("XDG_RUNTIME_DIR", "")
        if not runtime_value:
            raise DaemonControlError("runtime-unavailable")
        runtime_root = Path(runtime_value)
        if not runtime_root.is_absolute():
            raise DaemonControlError("runtime-unavailable")
        self._require_private_directory(runtime_root)

        path = self._requested_path or runtime_root / "murmur-ime" / "voice.sock"
        if not path.is_absolute():
            raise DaemonControlError("socket-unavailable")
        path = Path(os.path.abspath(path))
        try:
            path.relative_to(Path(os.path.abspath(runtime_root)))
        except ValueError as error:
            raise DaemonControlError("socket-unavailable") from error

        self._require_private_directory(path.parent)
        try:
            metadata = path.lstat()
        except OSError as error:
            raise DaemonControlError("socket-unavailable") from error
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise DaemonControlError("socket-unavailable")
        return path

    @staticmethod
    def _require_private_directory(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise DaemonControlError("runtime-unavailable") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise DaemonControlError("runtime-unavailable")

    @staticmethod
    def _set_remaining_timeout(client: socket.socket, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        client.settimeout(remaining)

    @classmethod
    def _receive_response(cls, client: socket.socket, deadline: float) -> bytes:
        received = bytearray()
        while b"\n" not in received:
            cls._set_remaining_timeout(client, deadline)
            chunk = client.recv(min(512, MAX_RESPONSE_BYTES + 1 - len(received)))
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > MAX_RESPONSE_BYTES:
                raise DaemonControlError("response-too-large")
        if b"\n" not in received:
            raise DaemonControlError("invalid-response")
        return bytes(received).split(b"\n", 1)[0]

    @staticmethod
    def _parse_response(response: bytes) -> DaemonReply:
        def unique_object(pairs):
            document = {}
            for key, value in pairs:
                if key in document:
                    raise ValueError("duplicate JSON key")
                document[key] = value
            return document

        try:
            document = json.loads(
                response.decode("utf-8"), object_pairs_hook=unique_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise DaemonControlError("invalid-response") from error
        if not isinstance(document, dict):
            raise DaemonControlError("invalid-response")
        if set(document) != {"ok", "code", "state"}:
            raise DaemonControlError("invalid-response")
        ok = document.get("ok")
        code = document.get("code")
        state = document.get("state")
        if (
            not isinstance(ok, bool)
            or not isinstance(code, str)
            or _CODE_PATTERN.fullmatch(code) is None
            or not isinstance(state, str)
            or state not in STATES
        ):
            raise DaemonControlError("invalid-response")
        return DaemonReply(ok=ok, code=code, state=state)
