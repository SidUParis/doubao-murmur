"""Tests for strict IBus preedit delivery and engine restoration."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from gi.repository import GLib

from doubao_murmur.preedit_client import (
    PREEDIT_ENGINE,
    AcquireResult,
    PreeditClient,
)


class _IbusRunner:
    def __init__(self, engine: str = "rime") -> None:
        self.engine = engine
        self.calls: list[list[str]] = []
        self.fail_local = False

    def __call__(self, command, **_kwargs):
        command = list(command)
        self.calls.append(command)
        ibus_index = command.index("ibus")
        is_host = command[:ibus_index] == ["flatpak-spawn", "--host"]
        if self.fail_local and not is_host:
            raise OSError("sandbox ibus is unavailable")

        arguments = command[ibus_index + 1 :]
        if arguments == ["engine"]:
            return SimpleNamespace(stdout=f"{self.engine}\n")
        if len(arguments) == 2 and arguments[0] == "engine":
            self.engine = arguments[1]
            return SimpleNamespace(stdout="")
        raise AssertionError(f"unexpected ibus command: {command!r}")


class _Proxy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.responses: dict[str, bool] = {}
        self.fail_methods: set[str] = set()
        self.name_owner: str | None = ":1.4242"

    def get_name_owner(self):
        return self.name_owner

    def call_sync(self, method, parameters, *_args):
        unpacked = parameters.unpack()
        self.calls.append((method, unpacked))
        if method in self.fail_methods:
            # Include a marker resembling sensitive transcription to prove the
            # production logger never interpolates remote exception details.
            raise RuntimeError("remote reflected TOP-SECRET-TEXT")
        accepted = self.responses.get(method, True)
        return GLib.Variant("(b)", (accepted,))


def _client(
    runner: _IbusRunner | None = None,
    proxy: _Proxy | None = None,
    *,
    commands=None,
) -> tuple[PreeditClient, _IbusRunner, _Proxy, list[_Proxy]]:
    runner = runner or _IbusRunner()
    proxy = proxy or _Proxy()
    factory_calls: list[_Proxy] = []

    def proxy_factory():
        factory_calls.append(proxy)
        return proxy

    client = PreeditClient(
        proxy_factory=proxy_factory,
        command_provider=lambda _tool: commands or [["ibus"]],
        command_runner=runner,
        acquire_retry_seconds=0,
    )
    return client, runner, proxy, factory_calls


def test_acquire_saves_current_engine_and_switches_to_murmur_voice():
    client, runner, proxy, factory_calls = _client()

    assert client.current_engine() == "rime"
    assert client.acquire("utterance-1")

    assert runner.engine == PREEDIT_ENGINE
    assert client.active
    assert client.utterance_id == "utterance-1"
    assert client.original_engine == "rime"
    assert proxy.calls == [("Acquire", ("utterance-1",))]
    assert factory_calls == [proxy]


def test_engine_switch_uses_observed_state_when_ibus_returns_nonzero():
    class _NonzeroSetRunner(_IbusRunner):
        def __call__(self, command, **kwargs):
            result = super().__call__(command, **kwargs)
            arguments = list(command)[list(command).index("ibus") + 1 :]
            if len(arguments) == 2 and arguments[0] == "engine":
                assert kwargs["check"] is False
                result.returncode = 1
            return result

    runner = _NonzeroSetRunner()
    client, _runner, _proxy, _factory_calls = _client(runner=runner)

    assert client.acquire_result("utterance-1") is AcquireResult.ACQUIRED
    assert runner.engine == PREEDIT_ENGINE


def test_partial_requires_matching_utterance_and_strictly_newer_revision():
    client, _runner, proxy, _factory_calls = _client()
    assert client.acquire("utterance-1")

    assert client.partial("utterance-1", 1, "first")
    assert not client.partial("utterance-1", 1, "duplicate")
    assert not client.partial("utterance-1", 0, "older")
    assert not client.partial("different", 2, "wrong session")
    assert not client.partial("utterance-1", True, "boolean is not a revision")
    assert client.partial("utterance-1", 4, "newer")

    assert client.last_revision == 4
    assert proxy.calls == [
        ("Acquire", ("utterance-1",)),
        ("Partial", ("utterance-1", 1, "first")),
        ("Partial", ("utterance-1", 4, "newer")),
    ]


def test_rejected_partial_does_not_advance_revision():
    proxy = _Proxy()
    proxy.responses["Partial"] = False
    client, _runner, _proxy, _factory_calls = _client(proxy=proxy)
    assert client.acquire("utterance-1")

    assert not client.partial("utterance-1", 3, "draft")
    assert client.last_revision == 0


def test_final_commits_once_and_restores_original_engine():
    client, runner, proxy, factory_calls = _client()
    assert client.acquire("utterance-1")
    assert client.partial("utterance-1", 1, "draft")

    assert client.final("utterance-1", 2, "final")

    assert runner.engine == "rime"
    assert not client.active
    assert client.utterance_id is None
    assert not client.final("utterance-1", 3, "must not repeat")
    assert [method for method, _args in proxy.calls] == [
        "Acquire",
        "Partial",
        "Final",
    ]
    # Acquire, Partial, and Final all share exactly one proxy/sender.
    assert factory_calls == [proxy]


def test_invalid_final_keeps_session_alive_for_a_valid_final():
    client, runner, proxy, _factory_calls = _client()
    assert client.acquire("utterance-1")
    assert client.partial("utterance-1", 2, "draft")

    assert not client.final("utterance-1", 2, "stale")
    assert client.active
    assert runner.engine == PREEDIT_ENGINE
    assert client.final("utterance-1", 3, "final")

    assert [method for method, _args in proxy.calls].count("Final") == 1
    assert runner.engine == "rime"


def test_cancel_requires_matching_utterance_and_restores_engine():
    client, runner, proxy, _factory_calls = _client()
    assert client.acquire("utterance-1")

    assert not client.cancel("different")
    assert client.active
    assert client.cancel("utterance-1")

    assert runner.engine == "rime"
    assert not client.active
    assert proxy.calls[-1] == ("Cancel", ("utterance-1",))
    assert not client.cancel("utterance-1")


def test_rejected_acquire_restores_engine_without_enabling_paste_fallback():
    proxy = _Proxy()
    proxy.responses["Acquire"] = False
    client, runner, _proxy, _factory_calls = _client(proxy=proxy)

    assert client.acquire_result("utterance-1") is AcquireResult.REJECTED
    assert runner.engine == "rime"
    assert not client.active


def test_reachable_dbus_failure_is_rejected_without_paste_fallback():
    proxy = _Proxy()
    proxy.fail_methods.add("Acquire")
    client, runner, _proxy, _factory_calls = _client(proxy=proxy)

    assert client.acquire_result("utterance-1") is AcquireResult.REJECTED
    assert runner.engine == "rime"
    assert not client.active


def test_missing_dbus_owner_is_the_only_unavailable_result():
    proxy = _Proxy()
    proxy.name_owner = None
    proxy.fail_methods.add("Acquire")
    client, runner, _proxy, _factory_calls = _client(proxy=proxy)

    assert client.acquire_result("utterance-1") is AcquireResult.UNAVAILABLE
    assert runner.engine == "rime"
    assert not client.active


def test_acquire_retries_explicit_focus_rejection_then_succeeds():
    runner = _IbusRunner()
    proxy = _Proxy()
    responses = iter((False, False, True))

    def call_sync(method, parameters, *_args):
        proxy.calls.append((method, parameters.unpack()))
        return GLib.Variant("(b)", (next(responses),))

    proxy.call_sync = call_sync
    now = [0.0]

    def monotonic():
        return now[0]

    def sleeper(seconds):
        now[0] += seconds

    client = PreeditClient(
        proxy_factory=lambda: proxy,
        command_provider=lambda _tool: [["ibus"]],
        command_runner=runner,
        acquire_retry_seconds=0.2,
        acquire_retry_interval=0.05,
        monotonic=monotonic,
        sleeper=sleeper,
    )

    assert client.acquire_result("utterance-1") is AcquireResult.ACQUIRED
    assert [method for method, _args in proxy.calls] == [
        "Acquire",
        "Acquire",
        "Acquire",
    ]
    assert now[0] == 0.1


def test_acquire_retries_transient_dbus_failures_then_succeeds():
    runner = _IbusRunner()
    proxy = _Proxy()
    outcomes = iter((None, None, True))

    def call_sync(method, parameters, *_args):
        proxy.calls.append((method, parameters.unpack()))
        outcome = next(outcomes)
        if outcome is None:
            raise RuntimeError("temporary D-Bus race")
        return GLib.Variant("(b)", (outcome,))

    proxy.call_sync = call_sync
    now = [0.0]

    def sleeper(seconds):
        now[0] += seconds

    client = PreeditClient(
        proxy_factory=lambda: proxy,
        command_provider=lambda _tool: [["ibus"]],
        command_runner=runner,
        acquire_retry_seconds=0.2,
        acquire_retry_interval=0.05,
        monotonic=lambda: now[0],
        sleeper=sleeper,
    )

    assert client.acquire_result("utterance-1") is AcquireResult.ACQUIRED
    assert [method for method, _args in proxy.calls] == [
        "Acquire",
        "Acquire",
        "Acquire",
    ]
    assert now[0] == 0.1


def test_final_dbus_failure_still_restores_without_enabling_new_session_data():
    proxy = _Proxy()
    client, runner, _proxy, _factory_calls = _client(proxy=proxy)
    assert client.acquire("utterance-1")
    proxy.fail_methods.add("Final")

    assert not client.final("utterance-1", 1, "TOP-SECRET-TEXT")
    assert runner.engine == "rime"
    assert not client.active


def test_transcription_text_is_never_logged(caplog):
    proxy = _Proxy()
    client, _runner, _proxy, _factory_calls = _client(proxy=proxy)
    assert client.acquire("utterance-1")
    proxy.fail_methods.add("Partial")

    with caplog.at_level(logging.WARNING):
        assert not client.partial("utterance-1", 1, "TOP-SECRET-TEXT")

    assert "TOP-SECRET-TEXT" not in caplog.text
    assert "Partial" in caplog.text


def test_flatpak_host_ibus_is_used_consistently_when_available():
    runner = _IbusRunner()
    commands = [["ibus"], ["flatpak-spawn", "--host", "ibus"]]
    client, _runner, _proxy, _factory_calls = _client(runner=runner, commands=commands)

    assert client.acquire("utterance-1")
    assert runner.engine == PREEDIT_ENGINE
    assert not any(command[0] == "ibus" for command in runner.calls)
    assert ["flatpak-spawn", "--host", "ibus", "engine"] in runner.calls
    assert [
        "flatpak-spawn",
        "--host",
        "ibus",
        "engine",
        PREEDIT_ENGINE,
    ] in runner.calls

    assert client.cancel("utterance-1")
    assert runner.engine == "rime"


def test_failed_restore_is_retained_and_retried_on_close():
    class _RestoreRunner(_IbusRunner):
        fail_restore = False

        def __call__(self, command, **kwargs):
            arguments = list(command)[list(command).index("ibus") + 1 :]
            if self.fail_restore and arguments == ["engine", "rime"]:
                self.calls.append(list(command))
                return SimpleNamespace(stdout="", returncode=1)
            return super().__call__(command, **kwargs)

    runner = _RestoreRunner()
    proxy = _Proxy()
    now = [0.0]
    client = PreeditClient(
        proxy_factory=lambda: proxy,
        command_provider=lambda _tool: [["ibus"]],
        command_runner=runner,
        acquire_retry_seconds=0,
        acquire_retry_interval=0.05,
        monotonic=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert client.acquire("utterance-1")
    runner.fail_restore = True

    assert client.final("utterance-1", 1, "final")
    assert client.restore_pending
    assert runner.engine == PREEDIT_ENGINE

    runner.fail_restore = False
    client.close()
    assert runner.engine == "rime"
    assert not client.restore_pending


def test_already_selected_murmur_voice_is_not_switched_or_restored():
    runner = _IbusRunner(PREEDIT_ENGINE)
    client, _runner, proxy, _factory_calls = _client(runner=runner)

    assert client.acquire("utterance-1")
    assert client.final("utterance-1", 1, "final")

    assert runner.calls == [["ibus", "engine"]]
    assert [method for method, _args in proxy.calls] == ["Acquire", "Final"]


def test_nested_and_malformed_acquisitions_are_rejected_without_switching():
    client, runner, proxy, _factory_calls = _client()

    assert not client.acquire("")
    assert not client.acquire("bad\x00id")
    assert client.acquire("utterance-1")
    assert not client.acquire("utterance-2")

    assert [method for method, _args in proxy.calls] == ["Acquire"]
    assert runner.engine == PREEDIT_ENGINE


def test_close_cancels_active_utterance_and_restores_engine():
    client, runner, proxy, _factory_calls = _client()
    assert client.acquire("utterance-1")

    client.close()

    assert runner.engine == "rime"
    assert not client.active
    assert proxy.calls[-1] == ("Cancel", ("utterance-1",))
