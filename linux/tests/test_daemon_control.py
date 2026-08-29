"""Protocol and filesystem-boundary tests for the controller-only sidecar."""

from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from doubao_murmur.daemon_control import (
    MAX_RESPONSE_BYTES,
    DaemonController,
    DaemonControlError,
    DaemonReply,
)


def _runtime(tmp_path):
    runtime = tmp_path / "runtime"
    socket_dir = runtime / "murmur-ime"
    socket_dir.mkdir(parents=True)
    runtime.chmod(0o700)
    socket_dir.chmod(0o700)
    return runtime, socket_dir / "voice.sock"


@contextmanager
def _fake_daemon(path, response: bytes, *, delay: float = 0.0):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(1)
    received: list[bytes] = []

    def serve():
        connection, _ = server.accept()
        with connection:
            received.append(connection.recv(256))
            if delay:
                time.sleep(delay)
            try:
                connection.sendall(response)
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield received
    finally:
        thread.join(timeout=1)
        server.close()
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("command", ["start", "stop", "toggle", "cancel", "status"])
def test_allowlisted_commands_roundtrip_over_private_socket(tmp_path, command):
    runtime, path = _runtime(tmp_path)
    response = b'{"ok":true,"code":"status","state":"idle"}\n'
    with _fake_daemon(path, response) as received:
        reply = DaemonController(
            path, environ={"XDG_RUNTIME_DIR": str(runtime)}
        ).request(command)

    assert received == [(command + "\n").encode("ascii")]
    assert reply == DaemonReply(ok=True, code="status", state="idle")


def test_rejects_unknown_command_without_opening_socket(tmp_path):
    runtime, path = _runtime(tmp_path)
    controller = DaemonController(path, environ={"XDG_RUNTIME_DIR": str(runtime)})

    with pytest.raises(DaemonControlError) as raised:
        controller.request("shutdown")

    assert raised.value.code == "invalid-command"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (b"not-json\n", "invalid-response"),
        (b'{"ok":true,"code":"status","state":"unknown"}\n', "invalid-response"),
        (b'{"ok":"yes","code":"status","state":"idle"}\n', "invalid-response"),
        (b'{"ok":true,"code":"STATUS","state":"idle"}\n', "invalid-response"),
        (b'{"ok":true,"code":"status","state":[]}\n', "invalid-response"),
        (
            b'{"ok":false,"ok":true,"code":"status","state":"idle"}\n',
            "invalid-response",
        ),
        (
            b'{"ok":true,"code":"status","state":"idle","text":"secret"}\n',
            "invalid-response",
        ),
        (b'{"ok":true,"code":"status","state":"idle"}', "invalid-response"),
        (b"x" * (MAX_RESPONSE_BYTES + 1) + b"\n", "response-too-large"),
    ],
)
def test_rejects_malformed_or_unbounded_response(tmp_path, response, expected):
    runtime, path = _runtime(tmp_path)
    with _fake_daemon(path, response):
        with pytest.raises(DaemonControlError) as raised:
            DaemonController(path, environ={"XDG_RUNTIME_DIR": str(runtime)}).request(
                "status"
            )

    assert raised.value.code == expected


def test_times_out_without_blocking_forever(tmp_path):
    runtime, path = _runtime(tmp_path)
    response = b'{"ok":true,"code":"status","state":"idle"}\n'
    with _fake_daemon(path, response, delay=0.2):
        with pytest.raises(DaemonControlError) as raised:
            DaemonController(
                path,
                timeout=0.05,
                environ={"XDG_RUNTIME_DIR": str(runtime)},
            ).request("status")

    assert raised.value.code == "request-timeout"


def test_timeout_is_one_total_deadline_not_reset_by_trickle_bytes(tmp_path):
    runtime, path = _runtime(tmp_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(1)

    def serve():
        connection, _ = server.accept()
        with connection:
            connection.recv(256)
            for chunk in (
                b'{"ok":',
                b"true,",
                b'"code":"status",',
                b'"state":"idle"}\n',
            ):
                time.sleep(0.04)
                try:
                    connection.sendall(chunk)
                except OSError:
                    return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(DaemonControlError) as raised:
            DaemonController(
                path,
                timeout=0.05,
                environ={"XDG_RUNTIME_DIR": str(runtime)},
            ).request("status")
    finally:
        thread.join(timeout=1)
        server.close()
        path.unlink(missing_ok=True)

    assert raised.value.code == "request-timeout"
    assert time.monotonic() - started < 0.15


def test_refuses_socket_outside_runtime_directory(tmp_path):
    runtime, _ = _runtime(tmp_path)
    outside = tmp_path / "outside.sock"
    controller = DaemonController(outside, environ={"XDG_RUNTIME_DIR": str(runtime)})

    with pytest.raises(DaemonControlError) as raised:
        controller.request("status")

    assert raised.value.code == "socket-unavailable"


def test_refuses_public_or_symlinked_socket(tmp_path):
    runtime, path = _runtime(tmp_path)
    response = b'{"ok":true,"code":"status","state":"idle"}\n'
    with _fake_daemon(path, response):
        path.chmod(0o666)
        with pytest.raises(DaemonControlError) as raised:
            DaemonController(path, environ={"XDG_RUNTIME_DIR": str(runtime)}).request(
                "status"
            )
    assert raised.value.code == "socket-unavailable"

    target = path.parent / "target.sock"
    with _fake_daemon(target, response):
        path.symlink_to(target.name)
        with pytest.raises(DaemonControlError) as raised:
            DaemonController(path, environ={"XDG_RUNTIME_DIR": str(runtime)}).request(
                "status"
            )
    assert raised.value.code == "socket-unavailable"


def test_refuses_non_private_runtime_directory(tmp_path):
    runtime, path = _runtime(tmp_path)
    runtime.chmod(0o755)

    with pytest.raises(DaemonControlError) as raised:
        DaemonController(path, environ={"XDG_RUNTIME_DIR": str(runtime)}).request(
            "status"
        )

    assert raised.value.code == "runtime-unavailable"
