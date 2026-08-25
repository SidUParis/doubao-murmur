"""Offline tests for dynamic physical-microphone selection."""

from __future__ import annotations

import builtins
import json
import os
import threading
import time

import pytest

from doubao_murmur.audio_capture import (
    AudioCapture,
    AudioDeviceError,
    MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS,
    _PreflightBudget,
    _PulseInputSelection,
    _load_sounddevice,
    _resolve_pulse_portaudio_device,
    resolve_input_device as _resolve_input_device,
)


class FakePulseSoundDevice:
    def __init__(self, devices=None, rejected=()):
        self.devices = (
            [{"name": "pulse", "max_input_channels": 32}]
            if devices is None
            else devices
        )
        self.rejected = set(rejected)
        self.checked = []

    def query_devices(self, kind=None):
        assert kind is None
        return self.devices

    def check_input_settings(self, **options):
        self.checked.append(options)
        if options.get("device") in self.rejected:
            raise RuntimeError("unsupported")


def resolve_input_device(**kwargs):
    if kwargs.get("pactl_runner") is not None and "sounddevice_module" not in kwargs:
        kwargs["sounddevice_module"] = FakePulseSoundDevice()
    return _resolve_input_device(**kwargs)


class FakeStream:
    def __init__(self, **options):
        self.options = options
        self.active = False
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True


class FakePulse:
    def __init__(self, *, sources: str, default: str, cards=None):
        self.sources = sources
        self.initial_sources = sources
        self.default = default
        self.cards = cards or []
        self.after_profile_sources: str | None = None
        self.after_profile_json_sources = []
        self.calls: list[tuple[str, ...]] = []
        self.fail: set[tuple[str, ...]] = set()
        self.concurrent_default_on_source_identity_read = None
        self.concurrent_default_on_cards_read = None
        self.concurrent_profile_on_cards_read = None
        self._cards_reads = 0

    def __call__(self, arguments):
        command = tuple(arguments)
        self.calls.append(command)
        if command in self.fail:
            raise RuntimeError("simulated pactl failure")
        if command == ("list", "short", "sources"):
            return self.sources
        if command == ("get-default-source",):
            return f"{self.default}\n" if self.default else ""
        if command == ("info",):
            return f"Default Source: {self.default}\n"
        if command == ("--format=json", "list", "cards"):
            self._cards_reads += 1
            if (
                self.concurrent_default_on_cards_read is not None
                and self._cards_reads == self.concurrent_default_on_cards_read[0]
            ):
                self.default = self.concurrent_default_on_cards_read[1]
            if (
                self.concurrent_profile_on_cards_read is not None
                and self._cards_reads == self.concurrent_profile_on_cards_read[0]
            ):
                for card in self.cards:
                    if card.get("name") == "alsa_card.pci-test":
                        card["active_profile"] = self.concurrent_profile_on_cards_read[
                            1
                        ]
            return json.dumps(self.cards)
        if command == ("--format=json", "list", "sources"):
            if self.concurrent_default_on_source_identity_read is not None:
                self.default = self.concurrent_default_on_source_identity_read
                self.concurrent_default_on_source_identity_read = None
            return json.dumps(self.after_profile_json_sources)
        if command[:1] == ("set-card-profile",):
            for card in self.cards:
                if card.get("name") == command[1]:
                    card["active_profile"] = command[2]
            if "+input:" in command[2] and self.after_profile_sources is not None:
                self.sources = self.after_profile_sources
            else:
                self.sources = self.initial_sources
            return ""
        raise AssertionError(f"unexpected pactl call: {command!r}")


def _source(index: int, name: str, state: str = "SUSPENDED") -> str:
    return f"{index}\t{name}\tPipeWire\ts32le 2ch 48000Hz\t{state}\n"


def _assert_pulse_selection(selection, source, portaudio_device=0):
    assert isinstance(selection, _PulseInputSelection)
    assert selection.source == source
    assert selection.portaudio_device == portaudio_device


def _json_source(
    name: str,
    card_index: int | None,
    state: str = "SUSPENDED",
    device_class: str = "sound",
    *,
    device_name: str | None = None,
    alsa_card: str | None = None,
    bus_path: str | None = None,
):
    properties = {
        "device.class": device_class,
        "media.class": "Audio/Source",
    }
    if device_name is not None:
        properties["device.name"] = device_name
    if alsa_card is not None:
        properties["alsa.card"] = alsa_card
    if bus_path is not None:
        properties["device.bus_path"] = bus_path
    return {
        "name": name,
        "card": card_index,
        "state": state,
        "properties": properties,
    }


def _output_only_card(
    name: str = "alsa_card.pci-test",
    *,
    index: int = 2,
    priority: int = 6565,
    candidate_sources: int = 1,
    alsa_card: str | None = None,
    bus_path: str | None = None,
):
    card = {
        "name": name,
        "index": index,
        "active_profile": "output:analog-stereo",
        "profiles": {
            "output:analog-stereo": {
                "sinks": 1,
                "sources": 0,
                "priority": 6500,
                "available": True,
            },
            "output:analog-stereo+input:analog-stereo": {
                "sinks": 1,
                "sources": candidate_sources,
                "priority": priority,
                "available": True,
            },
            # This profile changes the output route, so automatic recovery
            # must never select it even though its priority is higher.
            "output:hdmi-stereo+input:analog-stereo": {
                "sinks": 1,
                "sources": 1,
                "priority": 9999,
                "available": True,
            },
        },
    }
    properties = {}
    if alsa_card is not None:
        properties["alsa.card"] = alsa_card
    if bus_path is not None:
        properties["device.bus_path"] = bus_path
    if properties:
        card["properties"] = properties
    return card


def test_audio_capture_import_does_not_load_sounddevice():
    capture = AudioCapture()
    assert not capture.is_capturing


def test_load_sounddevice_error_is_actionable(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="sounddevice/PortAudio"):
        _load_sounddevice()


def test_each_recording_resolves_the_input_again():
    devices = iter((3, 8))
    streams = []

    def factory(**options):
        stream = FakeStream(**options)
        streams.append(stream)
        return stream

    capture = AudioCapture(
        input_resolver=lambda: next(devices),
        stream_factory=factory,
    )
    capture.start(lambda chunk: None)
    capture.stop()
    capture.start(lambda chunk: None)
    capture.stop()

    assert [stream.options["device"] for stream in streams] == [3, 8]


def test_pulse_route_is_present_through_factory_and_start_then_restored(monkeypatch):
    source = "alsa_input.pci-test.analog-stereo"
    monkeypatch.setenv("PULSE_SOURCE", "preexisting.source")
    observed = []

    class InspectingStream(FakeStream):
        def start(self):
            observed.append(("start", os.environ.get("PULSE_SOURCE")))
            super().start()

    def factory(**options):
        observed.append(("factory", os.environ.get("PULSE_SOURCE")))
        return InspectingStream(**options)

    capture = AudioCapture(
        input_resolver=lambda: _PulseInputSelection(source, 17),
        stream_factory=factory,
    )
    capture.start(lambda chunk: None)

    assert observed == [("factory", source), ("start", source)]
    assert capture._stream.options["device"] == 17
    assert os.environ["PULSE_SOURCE"] == "preexisting.source"
    capture.stop()


@pytest.mark.parametrize("failure_phase", ["factory", "start"])
def test_pulse_route_environment_is_restored_after_open_failure(
    monkeypatch, failure_phase
):
    source = "alsa_input.pci-test.analog-stereo"
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    stream = FakeStream()

    if failure_phase == "start":

        def fail_start():
            assert os.environ.get("PULSE_SOURCE") == source
            raise RuntimeError("simulated failure")

        stream.start = fail_start

    def factory(**options):
        assert os.environ.get("PULSE_SOURCE") == source
        if failure_phase == "factory":
            raise RuntimeError("simulated failure")
        stream.options = options
        return stream

    capture = AudioCapture(
        input_resolver=lambda: _PulseInputSelection(source, 17),
        stream_factory=factory,
    )

    with pytest.raises(AudioDeviceError, match="无法打开"):
        capture.start(lambda chunk: None)

    assert "PULSE_SOURCE" not in os.environ
    if failure_phase == "start":
        assert stream.closed


def test_nonpulse_open_waits_for_concurrent_pulse_environment(monkeypatch):
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    first_entered = threading.Event()
    release_first = threading.Event()
    observations = []

    class BlockingStream(FakeStream):
        def __init__(self, route, **options):
            super().__init__(**options)
            self.route = route

        def start(self):
            observations.append(("start", self.route, os.environ.get("PULSE_SOURCE")))
            if self.route == "alsa_input.first":
                first_entered.set()
                assert release_first.wait(timeout=1)
            super().start()

    def factory(**options):
        source = os.environ.get("PULSE_SOURCE")
        route = source or f"device:{options.get('device')}"
        observations.append(("factory", route, source))
        return BlockingStream(route, **options)

    first = AudioCapture(
        input_resolver=lambda: _PulseInputSelection("alsa_input.first", 17),
        stream_factory=factory,
    )
    second = AudioCapture(input_resolver=lambda: 7, stream_factory=factory)
    threads = [
        threading.Thread(target=capture.start, args=(lambda chunk: None,))
        for capture in (first, second)
    ]
    threads[0].start()
    assert first_entered.wait(timeout=1)
    threads[1].start()
    time.sleep(0.05)
    assert [item[1] for item in observations] == [
        "alsa_input.first",
        "alsa_input.first",
    ]
    release_first.set()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()

    assert observations == [
        ("factory", "alsa_input.first", "alsa_input.first"),
        ("start", "alsa_input.first", "alsa_input.first"),
        ("factory", "device:7", None),
        ("start", "device:7", None),
    ]
    assert "PULSE_SOURCE" not in os.environ
    first.stop()
    second.stop()


def test_valid_physical_default_is_left_untouched():
    microphone = "alsa_input.pci-test.analog-stereo"
    pulse = FakePulse(
        sources=_source(1, microphone) + _source(2, "sink.monitor"),
        default=microphone,
    )

    _assert_pulse_selection(resolve_input_device(pactl_runner=pulse), microphone)
    assert not [call for call in pulse.calls if call[0].startswith("set-")]


def test_monitor_default_is_left_unchanged_and_source_is_bound_per_stream():
    microphone = "alsa_input.pci-test.analog-stereo"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor") + _source(2, microphone),
        default="sink.monitor",
    )

    _assert_pulse_selection(resolve_input_device(pactl_runner=pulse), microphone)
    assert pulse.default == "sink.monitor"
    assert not [call for call in pulse.calls if call[0].startswith("set-")]


def test_unobservable_initial_default_fails_before_any_global_mutation():
    microphone = "alsa_input.pci-test.analog-stereo"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor") + _source(2, microphone),
        default="sink.monitor",
    )
    pulse.fail.update({("get-default-source",), ("info",)})

    with pytest.raises(AudioDeviceError, match="无法确定当前默认麦克风"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert not any(call[0].startswith("set-") for call in pulse.calls)


def test_real_disconnect_scenario_recovers_matching_duplex_profile():
    """A monitor-only default and output-only built-in card are repaired."""

    microphone = "alsa_input.pci-test.analog-stereo"
    pulse = FakePulse(
        sources=_source(1, "alsa_output.pci-test.analog-stereo.monitor"),
        default="alsa_output.pci-test.analog-stereo.monitor",
        cards=[_output_only_card()],
    )
    pulse.after_profile_sources = pulse.sources + _source(2, microphone)
    pulse.after_profile_json_sources = [_json_source(microphone, 2)]

    selection = resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)
    _assert_pulse_selection(selection, microphone)
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo+input:analog-stereo",
    ) in pulse.calls
    assert pulse.default == "alsa_output.pci-test.analog-stereo.monitor"
    assert not any(call[0] == "set-default-source" for call in pulse.calls)
    assert not any(
        "mute" in part or "volume" in part for call in pulse.calls for part in call
    )


def test_pipewire_null_card_uses_matching_device_name_identity():
    microphone = "alsa_input.pci-test.analog-stereo"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    pulse.after_profile_sources = _source(2, microphone)
    pulse.after_profile_json_sources = [
        _json_source(microphone, None, device_name="alsa_card.pci-test")
    ]

    selection = resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    _assert_pulse_selection(selection, microphone)
    assert pulse.default == "sink.monitor"


def test_pulseaudio_15_null_card_uses_alsa_and_bus_path_identity():
    microphone = "alsa_input.pci-test.analog-stereo"
    bus_path = "pci-0000:00:1f.3"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card(index=1, alsa_card="0", bus_path=bus_path)],
    )
    pulse.after_profile_sources = _source(2, microphone)
    pulse.after_profile_json_sources = [
        _json_source(microphone, None, alsa_card="0", bus_path=bus_path)
    ]

    selection = resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    _assert_pulse_selection(selection, microphone)
    assert pulse.default == "sink.monitor"


@pytest.mark.parametrize(
    ("source_alsa_card", "source_bus_path"),
    [
        ("0", None),
        (None, "pci-0000:00:1f.3"),
        ("1", "pci-0000:00:1f.3"),
        ("0", "pci-0000:00:1e.0"),
    ],
)
def test_pulseaudio_15_identity_missing_or_conflicting_fails_closed(
    source_alsa_card, source_bus_path
):
    microphone = "alsa_input.pci-test.analog-stereo"
    bus_path = "pci-0000:00:1f.3"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card(index=1, alsa_card="0", bus_path=bus_path)],
    )
    pulse.after_profile_sources = _source(2, microphone)
    pulse.after_profile_json_sources = [
        _json_source(
            microphone,
            None,
            alsa_card=source_alsa_card,
            bus_path=source_bus_path,
        )
    ]

    with pytest.raises(AudioDeviceError, match="仍未出现物理麦克风"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert pulse.cards[0]["active_profile"] == "output:analog-stereo"
    assert ("set-default-source", microphone) not in pulse.calls


def test_conflicting_numeric_and_named_card_identity_is_rejected():
    microphone = "alsa_input.pci-test.analog-stereo"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    pulse.after_profile_sources = _source(2, microphone)
    pulse.after_profile_json_sources = [
        _json_source(microphone, 2, device_name="alsa_card.someone-else")
    ]

    with pytest.raises(AudioDeviceError, match="仍未出现物理麦克风"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert pulse.cards[0]["active_profile"] == "output:analog-stereo"
    assert ("set-default-source", microphone) not in pulse.calls


def test_two_sources_on_recovered_card_are_never_guessed():
    first = "alsa_input.pci-test.analog-stereo"
    second = "alsa_input.pci-test.alt"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    pulse.after_profile_sources = _source(2, first) + _source(3, second)
    pulse.after_profile_json_sources = [
        _json_source(first, 2),
        _json_source(second, 2),
    ]

    with pytest.raises(AudioDeviceError, match="多个物理麦克风"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert pulse.cards[0]["active_profile"] == "output:analog-stereo"
    assert not any(call[0] == "set-default-source" for call in pulse.calls)


def test_monitor_only_without_a_safe_alsa_recovery_fails_closed():
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card("bluez_card.headset")],
    )

    with pytest.raises(AudioDeviceError, match="没有可安全恢复"):
        resolve_input_device(pactl_runner=pulse)
    assert not [call for call in pulse.calls if call[0].startswith("set-")]


def test_recovery_that_still_has_no_physical_source_rolls_back_and_fails():
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    pulse.after_profile_sources = _source(1, "sink.monitor")

    with pytest.raises(AudioDeviceError, match="仍未出现物理麦克风"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo+input:analog-stereo",
    ) in pulse.calls
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) in pulse.calls
    assert ("set-default-source", "sink.monitor") not in pulse.calls


def test_no_default_attempt_preserves_profile_after_concurrent_default_change():
    concurrent_default = "bluez_input.user-choice"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    pulse.concurrent_default_on_source_identity_read = concurrent_default

    with pytest.raises(AudioDeviceError, match="仍未出现物理麦克风"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert pulse.default == concurrent_default
    assert pulse.cards[0]["active_profile"] == (
        "output:analog-stereo+input:analog-stereo"
    )
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) not in pulse.calls


def test_recovery_ignores_a_concurrent_source_from_another_card():
    microphone = "alsa_input.pci-test.analog-stereo"
    concurrent = "xrdp_input.concurrent"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    pulse.after_profile_sources = _source(2, concurrent) + _source(3, microphone)
    pulse.after_profile_json_sources = [
        _json_source(concurrent, 17),
        _json_source(microphone, 2),
    ]

    selection = resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    _assert_pulse_selection(selection, microphone)
    assert pulse.default == "sink.monitor"
    assert not any(call[0] == "set-default-source" for call in pulse.calls)


def test_unrelated_source_does_not_leave_changed_card_profile():
    concurrent = "xrdp_input.concurrent"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    pulse.after_profile_sources = _source(2, concurrent)
    pulse.after_profile_json_sources = [_json_source(concurrent, 17)]

    with pytest.raises(AudioDeviceError, match="仍未出现物理麦克风"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert pulse.cards[0]["active_profile"] == "output:analog-stereo"
    assert ("set-default-source", concurrent) not in pulse.calls


def test_multiple_recoverable_alsa_cards_are_not_guessed():
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[
            _output_only_card("alsa_card.builtin", index=2),
            _output_only_card("alsa_card.usb", index=3),
        ],
    )

    with pytest.raises(AudioDeviceError, match="多个可恢复的声卡"):
        resolve_input_device(pactl_runner=pulse)
    assert not [call for call in pulse.calls if call[0].startswith("set-")]


def test_profile_declaring_multiple_sources_fails_before_mutation():
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card(candidate_sources=2)],
    )

    with pytest.raises(AudioDeviceError, match="没有可安全恢复"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert not any(call[0].startswith("set-") for call in pulse.calls)


def test_multiple_physical_sources_with_monitor_default_fail_closed():
    pulse = FakePulse(
        sources=(
            _source(1, "sink.monitor")
            + _source(2, "alsa_input.builtin")
            + _source(3, "alsa_input.usb")
        ),
        default="sink.monitor",
    )

    with pytest.raises(AudioDeviceError, match="多个物理麦克风"):
        resolve_input_device(pactl_runner=pulse)
    assert ("set-default-source", "alsa_input.builtin") not in pulse.calls
    assert ("set-default-source", "alsa_input.usb") not in pulse.calls


def test_failed_recovery_preserves_concurrent_profile_change():
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    pulse.concurrent_profile_on_cards_read = (2, "output:hdmi-stereo")

    with pytest.raises(AudioDeviceError, match="仍未出现物理麦克风"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert pulse.cards[0]["active_profile"] == "output:hdmi-stereo"
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) not in pulse.calls


def test_failed_recovery_rechecks_default_before_profile_rollback():
    concurrent_default = "bluez_input.late-user-choice"
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    pulse.concurrent_default_on_cards_read = (3, concurrent_default)

    with pytest.raises(AudioDeviceError, match="仍未出现物理麦克风"):
        resolve_input_device(pactl_runner=pulse, sleep=lambda seconds: None)

    assert pulse.default == concurrent_default
    assert pulse.cards[0]["active_profile"] == (
        "output:analog-stereo+input:analog-stereo"
    )
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) not in pulse.calls


def test_pactl_disappearing_after_first_probe_never_uses_generic_fallback():
    microphone = "alsa_input.pci-test.analog-stereo"
    pulse = FakePulse(sources=_source(1, microphone), default=microphone)

    def disappear_after_probe(arguments):
        if tuple(arguments) == ("get-default-source",):
            raise FileNotFoundError
        return pulse(arguments)

    with pytest.raises(AudioDeviceError, match="检查期间消失"):
        _resolve_input_device(
            pactl_runner=disappear_after_probe,
            sounddevice_module=FakePulseSoundDevice(),
        )


def test_applied_profile_then_pactl_disappearance_rolls_back_and_fails_closed():
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    new_profile = (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo+input:analog-stereo",
    )

    def apply_then_disappear(arguments):
        result = pulse(arguments)
        if tuple(arguments) == new_profile:
            raise FileNotFoundError
        return result

    with pytest.raises(AudioDeviceError, match="检查期间消失"):
        _resolve_input_device(
            pactl_runner=apply_then_disappear,
            sounddevice_module=FakePulseSoundDevice(),
            sleep=lambda seconds: None,
        )

    assert pulse.cards[0]["active_profile"] == "output:analog-stereo"


def test_resolution_failure_never_opens_a_stream():
    opened = []

    def fail():
        raise AudioDeviceError("没有可用的物理麦克风")

    capture = AudioCapture(
        input_resolver=fail,
        stream_factory=lambda **options: opened.append(options),
    )

    with pytest.raises(AudioDeviceError, match="没有可用"):
        capture.start(lambda chunk: None)
    assert opened == []


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_preflight_budget_reserves_rollback_time_and_has_hard_bound():
    clock = FakeClock()
    budget = _PreflightBudget(lambda arguments: "", clock.sleep, clock.monotonic)

    clock.now = 3.0
    with pytest.raises(AudioDeviceError, match="检查超时"):
        budget.forward(("info",))
    assert budget.rollback(("info",)) == ""

    clock.now = MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
    with pytest.raises(AudioDeviceError, match="检查超时"):
        budget.rollback(("info",))


def test_full_profile_rollback_fits_reserved_budget_with_info_fallback():
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    clock = FakeClock()

    def timed_runner(arguments):
        command = tuple(arguments)
        try:
            result = pulse(command)
            if (
                command == ("get-default-source",)
                and pulse.cards[0]["active_profile"]
                == "output:analog-stereo+input:analog-stereo"
            ):
                raise RuntimeError("force info fallback during rollback")
            return result
        finally:
            clock.now += 0.49

    with pytest.raises(AudioDeviceError, match="检查超时"):
        resolve_input_device(
            pactl_runner=timed_runner,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert clock.now < MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
    assert pulse.cards[0]["active_profile"] == "output:analog-stereo"
    assert pulse.calls.count(("info",)) == 2


def test_exhausted_rollback_budget_preserves_duplex_profile():
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )
    clock = FakeClock()

    def slow_after_profile(arguments):
        command = tuple(arguments)
        duplex = (
            pulse.cards[0]["active_profile"]
            == "output:analog-stereo+input:analog-stereo"
        )
        try:
            return pulse(command)
        finally:
            clock.now += 2.0 if duplex else 0.49

    with pytest.raises(AudioDeviceError, match="检查超时"):
        resolve_input_device(
            pactl_runner=slow_after_profile,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert clock.now >= MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
    assert pulse.cards[0]["active_profile"] == (
        "output:analog-stereo+input:analog-stereo"
    )
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) not in pulse.calls


def test_exact_unique_pulse_portaudio_device_is_returned_by_index():
    sounddevice = FakePulseSoundDevice(
        [
            {"name": "Built-in Audio", "max_input_channels": 2},
            {"name": "pulse", "max_input_channels": 32},
        ]
    )

    assert _resolve_pulse_portaudio_device(sounddevice) == 1
    assert sounddevice.checked == [
        {
            "device": 1,
            "channels": 1,
            "dtype": "int16",
            "samplerate": 16_000,
        }
    ]


@pytest.mark.parametrize(
    "sounddevice",
    [
        FakePulseSoundDevice([]),
        FakePulseSoundDevice(
            [
                {"name": "pulse", "max_input_channels": 32},
                {"name": "pulse", "max_input_channels": 32},
            ]
        ),
        FakePulseSoundDevice(
            [{"name": "pulse", "max_input_channels": 32}], rejected={0}
        ),
    ],
)
def test_missing_ambiguous_or_unsupported_pulse_endpoint_fails_closed(
    sounddevice,
):
    with pytest.raises(AudioDeviceError, match="没有唯一且可用的 PulseAudio"):
        _resolve_pulse_portaudio_device(sounddevice)


@pytest.mark.parametrize(
    "sounddevice",
    [
        FakePulseSoundDevice([]),
        FakePulseSoundDevice(
            [
                {"name": "pulse", "max_input_channels": 32},
                {"name": "pulse", "max_input_channels": 32},
            ]
        ),
    ],
)
def test_pulse_endpoint_is_verified_before_profile_mutation(sounddevice):
    pulse = FakePulse(
        sources=_source(1, "sink.monitor"),
        default="sink.monitor",
        cards=[_output_only_card()],
    )

    with pytest.raises(AudioDeviceError, match="没有唯一且可用的 PulseAudio"):
        _resolve_input_device(
            pactl_runner=pulse,
            sounddevice_module=sounddevice,
            sleep=lambda seconds: None,
        )

    assert not any(call[0].startswith("set-") for call in pulse.calls)


class FakeSoundDevice:
    def __init__(self, default, devices=()):
        self.default = default
        self.devices = list(devices)
        self.checked = []

    def query_devices(self, kind=None):
        return self.default if kind == "input" else self.devices

    def check_input_settings(self, **options):
        self.checked.append(options)


def _missing_pactl(arguments):
    del arguments
    raise FileNotFoundError


def test_missing_pactl_accepts_an_inspectable_physical_default():
    sounddevice = FakeSoundDevice(
        {
            "name": "Built-in physical microphone",
            "max_input_channels": 2,
            "index": 4,
        }
    )

    assert (
        resolve_input_device(
            pactl_runner=_missing_pactl,
            sounddevice_module=sounddevice,
        )
        == 4
    )


def test_unindexed_physical_default_is_frozen_only_after_unique_enumeration():
    sounddevice = FakeSoundDevice(
        {"name": "Built-in physical microphone", "max_input_channels": 2},
        devices=[{"name": "Built-in physical microphone", "max_input_channels": 2}],
    )

    assert (
        resolve_input_device(
            pactl_runner=_missing_pactl,
            sounddevice_module=sounddevice,
        )
        == 0
    )


def test_missing_pactl_rejects_a_generic_or_ambiguous_portaudio_route():
    sounddevice = FakeSoundDevice(
        {"name": "pulse", "max_input_channels": 32},
        devices=[
            {"name": "Built-in microphone", "max_input_channels": 2},
            {"name": "USB microphone", "max_input_channels": 1},
        ],
    )

    with pytest.raises(AudioDeviceError, match="没有唯一"):
        resolve_input_device(
            pactl_runner=_missing_pactl,
            sounddevice_module=sounddevice,
        )


@pytest.mark.parametrize(
    ("default", "devices"),
    [
        (
            {"name": "sink.monitor", "max_input_channels": 2, "index": 4},
            [{"name": "sink.monitor", "max_input_channels": 2}],
        ),
        (
            {
                "name": "Built-in microphone",
                "max_input_channels": 2,
                "index": "4",
            },
            [],
        ),
    ],
)
def test_missing_pactl_monitor_or_invalid_default_index_fails_closed(default, devices):
    with pytest.raises(AudioDeviceError, match="没有唯一"):
        resolve_input_device(
            pactl_runner=_missing_pactl,
            sounddevice_module=FakeSoundDevice(default, devices),
        )


def test_no_pactl_default_index_is_frozen_before_stream_open():
    sounddevice = FakeSoundDevice(
        {
            "name": "Built-in physical microphone",
            "max_input_channels": 2,
            "index": 4,
        }
    )
    streams = []

    def resolver():
        selected = _resolve_input_device(
            pactl_runner=_missing_pactl,
            sounddevice_module=sounddevice,
        )
        sounddevice.default["index"] = 9
        return selected

    capture = AudioCapture(
        input_resolver=resolver,
        stream_factory=lambda **options: (
            streams.append(FakeStream(**options)) or streams[-1]
        ),
    )
    capture.start(lambda chunk: None)

    assert streams[0].options["device"] == 4
    capture.stop()
