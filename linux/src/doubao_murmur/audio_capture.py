"""Microphone capture with conservative PulseAudio/PipeWire recovery.

The desktop default input can become a sink monitor after a Bluetooth headset
disconnects.  Resolve the input immediately before every recording so a long
running Flatpak does not keep using that stale route.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from doubao_murmur.config import AUDIO_BLOCKSIZE, AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from doubao_murmur.host_tools import command_candidates

logger = logging.getLogger(__name__)

_MAX_PACTL_OUTPUT = 512 * 1024
_SOURCE_APPEAR_DELAYS = (0.0, 0.05, 0.1, 0.2, 0.4, 0.8)
_PACTL_COMMAND_TIMEOUT_SECONDS = 0.5
_PREFLIGHT_FORWARD_TIMEOUT_SECONDS = 3.0
_PREFLIGHT_ROLLBACK_TIMEOUT_SECONDS = 7.0
MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS = (
    _PREFLIGHT_FORWARD_TIMEOUT_SECONDS + _PREFLIGHT_ROLLBACK_TIMEOUT_SECONDS
)
_PULSE_SOURCE_ENVIRONMENT_LOCK = threading.Lock()


class AudioDeviceError(RuntimeError):
    """A safe failure explaining why no physical microphone can be opened."""


class _PactlError(RuntimeError):
    """Internal marker for a failed pactl command."""


@dataclass(frozen=True)
class _PulseSource:
    name: str
    state: str
    card_index: int | None = None


@dataclass(frozen=True)
class _PulseInputSelection:
    """An exact Pulse source plus its already verified PortAudio endpoint."""

    source: str
    portaudio_device: int

    def __post_init__(self) -> None:
        if (
            _safe_identifier(self.source) != self.source
            or _pulse_index(self.portaudio_device) is None
        ):
            raise AudioDeviceError("无效的 PulseAudio 麦克风选择")


@dataclass(frozen=True)
class _ProfileChange:
    card: str
    card_index: int
    alsa_card: str | None
    bus_path: str | None
    old_profile: str
    new_profile: str
    priority: int


class _PreflightBudget:
    """Bound pactl recovery to 3 s forward plus 7 s reserved rollback."""

    def __init__(
        self,
        runner: Callable[[Sequence[str]], str] | None,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
    ) -> None:
        started = monotonic()
        self._forward_deadline = started + _PREFLIGHT_FORWARD_TIMEOUT_SECONDS
        self._hard_deadline = started + MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
        self._runner = runner
        self._sleep = sleep
        self._monotonic = monotonic

    def forward(self, arguments: Sequence[str]) -> str:
        return self._call(arguments, self._forward_deadline)

    def rollback(self, arguments: Sequence[str]) -> str:
        return self._call(arguments, self._hard_deadline)

    def pause(self, seconds: float) -> None:
        remaining = self._remaining(self._forward_deadline)
        if seconds >= remaining:
            raise AudioDeviceError("麦克风检查超时")
        self._sleep(seconds)
        self._remaining(self._forward_deadline)

    def _call(self, arguments: Sequence[str], deadline: float) -> str:
        remaining = self._remaining(deadline)
        try:
            if self._runner is None:
                result = _run_pactl(
                    arguments,
                    timeout=min(_PACTL_COMMAND_TIMEOUT_SECONDS, remaining),
                )
            else:
                result = self._runner(arguments)
        except Exception as error:
            if self._monotonic() >= deadline:
                raise AudioDeviceError("麦克风检查超时") from error
            raise
        self._remaining(deadline)
        return result

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise AudioDeviceError("麦克风检查超时")
        return remaining


class AudioCapture:
    """Captures microphone audio at 16 kHz mono Int16 PCM."""

    def __init__(
        self,
        *,
        input_resolver: Callable[[], _PulseInputSelection | int | str | None]
        | None = None,
        stream_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._stream: Any | None = None
        self._on_audio_data: Callable[[bytes], None] | None = None
        self._lock = threading.RLock()
        self._input_resolver = input_resolver or resolve_input_device
        self._stream_factory = stream_factory or _default_stream_factory

    @property
    def is_capturing(self) -> bool:
        with self._lock:
            stream = self._stream
        return bool(stream is not None and getattr(stream, "active", False))

    def start(self, on_audio_data: Callable[[bytes], None]) -> None:
        """Resolve a fresh physical input and start capturing from it."""

        if not callable(on_audio_data):
            raise TypeError("on_audio_data must be callable")
        with self._lock:
            if self._stream is not None:
                return

            # Deliberately run this on every start.  Pulse/PipeWire routes can
            # change while this long-running sidecar remains open.
            try:
                device = self._input_resolver()
            except AudioDeviceError:
                raise
            except Exception as error:
                raise AudioDeviceError("麦克风路由检查失败") from error

            options: dict[str, Any] = {
                "samplerate": AUDIO_SAMPLE_RATE,
                "channels": AUDIO_CHANNELS,
                "dtype": "int16",
                "blocksize": AUDIO_BLOCKSIZE,
                "callback": self._audio_callback,
                "latency": "low",
            }
            pulse_source: str | None = None
            if isinstance(device, _PulseInputSelection):
                pulse_source = device.source
                options["device"] = device.portaudio_device
            elif device is not None:
                options["device"] = device

            stream = None
            self._on_audio_data = on_audio_data
            try:
                # Serialize every PortAudio open so a simultaneous non-Pulse
                # stream cannot inherit another capture's brief PULSE_SOURCE.
                with _pulse_source_environment(pulse_source):
                    stream = self._stream_factory(**options)
                    stream.start()
            except Exception as error:
                self._on_audio_data = None
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                raise AudioDeviceError("无法打开选定的物理麦克风") from error
            self._stream = stream

        logger.info("Audio capture started: %d Hz mono", AUDIO_SAMPLE_RATE)

    def stop(self) -> None:
        """Stop capturing."""

        with self._lock:
            stream = self._stream
            self._stream = None
            self._on_audio_data = None
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()
        logger.info("Audio capture stopped")

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """Called by PortAudio on its audio thread."""

        del frames, time_info
        if status:
            logger.warning("Audio callback reported a capture status")
        with self._lock:
            callback = self._on_audio_data
        if callback is not None:
            callback(bytes(indata))

    @staticmethod
    def list_input_devices():
        """List available input devices for debugging."""

        return _load_sounddevice().query_devices(kind="input")

    @staticmethod
    def get_default_input_device():
        """Get the PortAudio default input device index."""

        return _load_sounddevice().default.device[0]


def resolve_input_device(
    *,
    pactl_runner: Callable[[Sequence[str]], str] | None = None,
    sounddevice_module: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> _PulseInputSelection | int | str | None:
    """Return a safe PortAudio input selection after checking Pulse routing.

    Pulse/PipeWire is preferred because it can distinguish a real source from
    a sink monitor.  If ``pactl`` is genuinely unavailable, fall back only to
    an inspectable and unambiguous PortAudio hardware input.
    """

    budget = _PreflightBudget(pactl_runner, sleep, monotonic)
    try:
        sources = _read_sources(budget.forward)
    except FileNotFoundError:
        return _resolve_unique_portaudio_input(
            sounddevice_module or _load_sounddevice()
        )
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("麦克风路由检查失败") from error

    try:
        pulse_device = _resolve_pulse_portaudio_device(sounddevice_module)
        return _resolve_pulse_input(
            budget.forward,
            budget.pause,
            budget.rollback,
            sources,
            pulse_device,
        )
    except FileNotFoundError as error:
        raise AudioDeviceError("PulseAudio 在麦克风检查期间消失") from error
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("麦克风路由检查失败") from error


def _resolve_pulse_input(
    runner: Callable[[Sequence[str]], str],
    sleep: Callable[[float], None],
    rollback_runner: Callable[[Sequence[str]], str],
    sources: Sequence[_PulseSource],
    pulse_device: int,
) -> _PulseInputSelection:
    real_sources = _physical_sources(sources)
    old_default = _read_default_source(runner)
    if old_default is None:
        # Never mutate a global route unless its previous value is observable
        # and can therefore be restored safely.
        raise AudioDeviceError("无法确定当前默认麦克风")

    if old_default and any(source.name == old_default for source in real_sources):
        return _PulseInputSelection(old_default, pulse_device)

    profile_change: _ProfileChange | None = None
    if not real_sources:
        profile_change = _find_unique_profile_change(_read_cards(runner))
        try:
            runner(
                (
                    "set-card-profile",
                    profile_change.card,
                    profile_change.new_profile,
                )
            )
        except Exception as error:
            # pactl may have applied the profile before reporting failure.
            _rollback_profile_change(rollback_runner, profile_change, old_default)
            if isinstance(error, FileNotFoundError):
                raise
            raise AudioDeviceError("无法恢复声卡的麦克风输入模式") from error

        try:
            real_sources = _wait_for_card_source(
                runner,
                sleep,
                profile_change,
            )
        except Exception:
            _rollback_profile_change(rollback_runner, profile_change, old_default)
            raise
        try:
            selected = _require_unique_source(real_sources)
        except Exception:
            _rollback_profile_change(rollback_runner, profile_change, old_default)
            raise
        # The same-output duplex profile intentionally remains active after
        # success; only this recording stream is bound to the recovered source.
        return _PulseInputSelection(selected.name, pulse_device)

    return _PulseInputSelection(
        _require_unique_source(real_sources).name,
        pulse_device,
    )


def _read_sources(
    runner: Callable[[Sequence[str]], str],
) -> tuple[_PulseSource, ...]:
    try:
        text = runner(("list", "short", "sources"))
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("无法读取系统麦克风列表") from error
    _check_output(text, "系统麦克风列表")

    sources: list[_PulseSource] = []
    invalid_lines = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            invalid_lines += 1
            continue
        name = _safe_identifier(fields[1].strip())
        if name is None:
            invalid_lines += 1
            continue
        state = fields[-1].strip().upper() if len(fields) >= 5 else "UNKNOWN"
        sources.append(_PulseSource(name, state))
    if invalid_lines and not sources:
        raise AudioDeviceError("系统返回了无效的麦克风列表")
    return tuple(sources)


def _physical_sources(sources: Sequence[_PulseSource]) -> tuple[_PulseSource, ...]:
    return tuple(source for source in sources if not _is_monitor(source.name))


def _is_monitor(name: str) -> bool:
    return name.casefold().endswith(".monitor")


def _read_default_source(runner: Callable[[Sequence[str]], str]) -> str | None:
    try:
        value = runner(("get-default-source",)).strip()
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception:
        value = ""
    direct = _safe_identifier(value)
    if direct is not None:
        return direct
    try:
        info = runner(("info",))
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception:
        return None
    _check_output(info, "PulseAudio 状态")
    for line in info.splitlines():
        label, separator, candidate = line.partition(":")
        if separator and label.strip() == "Default Source":
            return _safe_identifier(candidate.strip())
    return None


def _read_cards(runner: Callable[[Sequence[str]], str]) -> list[Any]:
    try:
        text = runner(("--format=json", "list", "cards"))
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("无法读取系统声卡配置") from error
    _check_output(text, "系统声卡配置")
    try:
        document = json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        raise AudioDeviceError("系统返回了无效的声卡配置") from error
    if not isinstance(document, list):
        raise AudioDeviceError("系统返回了无效的声卡配置")
    return document


def _read_card_sources(
    runner: Callable[[Sequence[str]], str], change: _ProfileChange
) -> tuple[_PulseSource, ...]:
    """Read sources bound by a strict Pulse/PipeWire card identity."""

    try:
        text = runner(("--format=json", "list", "sources"))
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("无法读取麦克风所属声卡") from error
    _check_output(text, "麦克风所属声卡")
    try:
        document = json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        raise AudioDeviceError("系统返回了无效的麦克风声卡信息") from error
    if not isinstance(document, list):
        raise AudioDeviceError("系统返回了无效的麦克风声卡信息")

    sources: list[_PulseSource] = []
    for item in document:
        if not isinstance(item, Mapping):
            continue
        name = _safe_identifier(item.get("name"))
        raw_card_index = item.get("card")
        source_card_index = _pulse_index(raw_card_index)
        properties = item.get("properties")
        if name is None or not isinstance(properties, Mapping):
            continue
        source_card_name = _safe_identifier(properties.get("device.name"))
        source_alsa_card = _safe_identifier(properties.get("alsa.card"))
        source_bus_path = _safe_identifier(properties.get("device.bus_path"))
        device_class = str(properties.get("device.class") or "").casefold()
        identity_conflicts = (
            (source_card_name is not None and source_card_name != change.card)
            or (
                change.alsa_card is not None
                and source_alsa_card is not None
                and source_alsa_card != change.alsa_card
            )
            or (
                change.bus_path is not None
                and source_bus_path is not None
                and source_bus_path != change.bus_path
            )
        )
        if raw_card_index is None:
            if source_card_name is not None:
                bound_to_card = source_card_name == change.card
            else:
                bound_to_card = (
                    change.alsa_card is not None
                    and change.bus_path is not None
                    and source_alsa_card == change.alsa_card
                    and source_bus_path == change.bus_path
                )
        elif source_card_index is None:
            bound_to_card = False
        else:
            bound_to_card = source_card_index == change.card_index
        if (
            not bound_to_card
            or identity_conflicts
            or _is_monitor(name)
            or device_class == "monitor"
        ):
            continue
        state = str(item.get("state") or "UNKNOWN").strip().upper()
        sources.append(_PulseSource(name, state, source_card_index))
    return tuple(sources)


def _find_unique_profile_change(cards: Sequence[Any]) -> _ProfileChange:
    per_card: list[_ProfileChange] = []
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        card_name = _safe_identifier(card.get("name"))
        card_index = _pulse_index(card.get("index"))
        properties = card.get("properties")
        alsa_card = (
            _safe_identifier(properties.get("alsa.card"))
            if isinstance(properties, Mapping)
            else None
        )
        bus_path = (
            _safe_identifier(properties.get("device.bus_path"))
            if isinstance(properties, Mapping)
            else None
        )
        profiles = card.get("profiles")
        active_name = _profile_name(card.get("active_profile"))
        if (
            card_name is None
            or card_index is None
            or not card_name.startswith("alsa_card.")
            or active_name is None
            or not isinstance(profiles, Mapping)
        ):
            continue

        active = profiles.get(active_name)
        if not isinstance(active, Mapping):
            continue
        sink_count = _nonnegative_integer(active.get("sinks"))
        source_count = _nonnegative_integer(active.get("sources"))
        if sink_count is None or sink_count < 1 or source_count != 0:
            continue

        paired: list[_ProfileChange] = []
        prefix = f"{active_name}+input:"
        for raw_name, metadata in profiles.items():
            name = _safe_identifier(raw_name)
            if (
                name is None
                or not name.startswith(prefix)
                or not isinstance(metadata, Mapping)
                or metadata.get("available") is not True
                or _nonnegative_integer(metadata.get("sinks")) != sink_count
            ):
                continue
            candidate_sources = _nonnegative_integer(metadata.get("sources"))
            priority = _nonnegative_integer(metadata.get("priority"))
            if candidate_sources != 1 or priority is None:
                continue
            paired.append(
                _ProfileChange(
                    card_name,
                    card_index,
                    alsa_card,
                    bus_path,
                    active_name,
                    name,
                    priority,
                )
            )

        if paired:
            best_priority = max(candidate.priority for candidate in paired)
            best = [
                candidate for candidate in paired if candidate.priority == best_priority
            ]
            if len(best) != 1:
                raise AudioDeviceError("声卡有多个等价的麦克风输入模式")
            per_card.append(best[0])

    if not per_card:
        raise AudioDeviceError("没有可安全恢复的物理麦克风")
    if len(per_card) != 1:
        raise AudioDeviceError("检测到多个可恢复的声卡，无法安全自动选择")
    return per_card[0]


def _profile_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("name")
    return _safe_identifier(value)


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or "\x00" in value or len(value) > 512:
        return None
    return value


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _pulse_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _wait_for_card_source(
    runner: Callable[[Sequence[str]], str],
    sleep: Callable[[float], None],
    change: _ProfileChange,
) -> tuple[_PulseSource, ...]:
    for delay in _SOURCE_APPEAR_DELAYS:
        if delay:
            sleep(delay)
        sources = _read_card_sources(runner, change)
        if sources:
            return sources
    raise AudioDeviceError("恢复声卡后仍未出现物理麦克风")


def _require_unique_source(sources: Sequence[_PulseSource]) -> _PulseSource:
    if not sources:
        raise AudioDeviceError("没有可用的物理麦克风")
    if len(sources) != 1:
        raise AudioDeviceError("检测到多个物理麦克风，无法安全自动选择")
    return sources[0]


def _rollback_profile_change(
    runner: Callable[[Sequence[str]], str],
    change: _ProfileChange,
    old_default: str,
) -> None:
    """Best-effort profile rollback that preserves unrecognized live state."""

    try:
        if _current_card_profile(runner, change.card) != change.new_profile:
            logger.warning("Audio-card profile changed concurrently; not rolling back")
            return
        try:
            live_default = _read_default_source(runner)
        except Exception:
            live_default = None
        if live_default != old_default:
            logger.warning(
                "Default microphone changed or is unreadable; preserving "
                "recovered profile"
            )
            return

        if _current_card_profile(runner, change.card) != change.new_profile:
            logger.warning("Audio-card profile changed concurrently; not rolling back")
            return
        if _read_default_source(runner) != old_default:
            logger.warning(
                "Default microphone changed or is unreadable; preserving "
                "recovered profile"
            )
            return
        runner(("set-card-profile", change.card, change.old_profile))
    except Exception:
        logger.error("Audio-card profile rollback failed")


@contextmanager
def _pulse_source_environment(source: str | None) -> Iterator[None]:
    """Serialize stream opens and optionally bind one exact Pulse source."""

    if source is not None and _safe_identifier(source) != source:
        raise AudioDeviceError("无效的 PulseAudio 麦克风 source")
    with _PULSE_SOURCE_ENVIRONMENT_LOCK:
        if source is None:
            yield
            return
        was_present = "PULSE_SOURCE" in os.environ
        previous = os.environ.get("PULSE_SOURCE")
        os.environ["PULSE_SOURCE"] = source
        try:
            yield
        finally:
            if was_present:
                assert previous is not None
                os.environ["PULSE_SOURCE"] = previous
            else:
                os.environ.pop("PULSE_SOURCE", None)


def _current_card_profile(
    runner: Callable[[Sequence[str]], str], card: str
) -> str | None:
    matches = [
        item
        for item in _read_cards(runner)
        if isinstance(item, Mapping) and _safe_identifier(item.get("name")) == card
    ]
    if len(matches) != 1:
        return None
    return _profile_name(matches[0].get("active_profile"))


def _check_output(text: Any, label: str) -> None:
    if not isinstance(text, str):
        raise AudioDeviceError(f"{label}无效")
    if len(text.encode("utf-8", errors="replace")) > _MAX_PACTL_OUTPUT:
        raise AudioDeviceError(f"{label}过大")


def _resolve_pulse_portaudio_device(sounddevice_module: Any | None = None) -> int:
    sounddevice_module = sounddevice_module or _load_sounddevice()
    try:
        devices = sounddevice_module.query_devices()
    except Exception as error:
        raise AudioDeviceError("无法读取 PulseAudio 的 PortAudio 输入") from error

    candidates: list[int] = []
    for index, device in enumerate(devices):
        if not isinstance(device, Mapping) or device.get("name") != "pulse":
            continue
        channels = _nonnegative_integer(device.get("max_input_channels"))
        if channels is None or channels < 1:
            continue
        try:
            sounddevice_module.check_input_settings(
                device=index,
                channels=AUDIO_CHANNELS,
                dtype="int16",
                samplerate=AUDIO_SAMPLE_RATE,
            )
        except Exception:
            continue
        candidates.append(index)
    if len(candidates) != 1:
        raise AudioDeviceError("没有唯一且可用的 PulseAudio PortAudio 输入")
    return candidates[0]


def _resolve_unique_portaudio_input(sounddevice_module: Any) -> int:
    try:
        default = sounddevice_module.query_devices(kind="input")
    except Exception:
        default = None
    default_index = (
        _pulse_index(default.get("index")) if isinstance(default, Mapping) else None
    )
    if _is_specific_input(default) and default_index is not None:
        try:
            sounddevice_module.check_input_settings(
                device=default_index,
                channels=AUDIO_CHANNELS,
                dtype="int16",
                samplerate=AUDIO_SAMPLE_RATE,
            )
        except Exception:
            pass
        else:
            return default_index

    try:
        devices = sounddevice_module.query_devices()
    except Exception as error:
        raise AudioDeviceError("无法读取 PortAudio 麦克风列表") from error
    candidates: list[int] = []
    for index, device in enumerate(devices):
        if not _is_specific_input(device):
            continue
        try:
            sounddevice_module.check_input_settings(
                device=index,
                channels=AUDIO_CHANNELS,
                dtype="int16",
                samplerate=AUDIO_SAMPLE_RATE,
            )
        except Exception:
            continue
        candidates.append(index)
    if len(candidates) != 1:
        raise AudioDeviceError("没有唯一且可验证的物理麦克风")
    return candidates[0]


def _is_specific_input(device: Any) -> bool:
    if not isinstance(device, Mapping):
        return False
    channels = _nonnegative_integer(device.get("max_input_channels"))
    name = str(device.get("name") or "").strip().casefold()
    if channels is None or channels < 1 or not name or "monitor" in name:
        return False
    return name not in {"default", "pulse", "pipewire"}


def _run_pactl(
    arguments: Sequence[str], *, timeout: float = _PACTL_COMMAND_TIMEOUT_SECONDS
) -> str:
    candidates = command_candidates("pactl")
    if not candidates:
        raise FileNotFoundError("pactl is unavailable")
    if timeout <= 0:
        raise _PactlError("pactl command timed out")

    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    last_error: Exception | None = None
    deadline = time.monotonic() + timeout
    for prefix in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _PactlError("pactl command timed out") from last_error
        try:
            completed = subprocess.run(
                [*prefix, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=remaining,
                check=False,
                env=environment,
            )
        except FileNotFoundError as error:
            last_error = error
            continue
        except (OSError, subprocess.SubprocessError) as error:
            last_error = error
            continue
        if completed.returncode == 0:
            return completed.stdout
        last_error = _PactlError("pactl command failed")
    raise _PactlError("pactl command failed") from last_error


def _default_stream_factory(**kwargs):
    return _load_sounddevice().RawInputStream(**kwargs)


def _load_sounddevice():
    try:
        import sounddevice as sd
    except Exception as error:
        raise RuntimeError(
            "sounddevice/PortAudio is not available; install sounddevice "
            "and PortAudio/PipeWire support before recording"
        ) from error
    return sd
