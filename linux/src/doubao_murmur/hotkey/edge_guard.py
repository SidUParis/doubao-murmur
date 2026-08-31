"""Bare right-Alt edge disambiguation for global key listeners.

The active keyboard layout may expose the physical right Alt key as AltGr.
Forwarding its key-down immediately would therefore start dictation for every
AltGr character.  This guard gives a chord a small arming window while still
forwarding the original monotonic key-down/key-up timestamps to the daemon.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

RIGHT_ALT_ARM_DELAY_SECONDS = 0.12


class RightAltEdgeGuard:
    """Forward a bare right-Alt hold as ordered press/release edges."""

    def __init__(
        self,
        on_press: Callable[[int], None],
        on_release: Callable[[int], None],
        on_cancel: Callable[[], None],
        *,
        arm_delay: float = RIGHT_ALT_ARM_DELAY_SECONDS,
        timer_factory: Callable[..., object] = threading.Timer,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel
        self._arm_delay = arm_delay
        self._timer_factory = timer_factory
        self._monotonic_ns = monotonic_ns
        self._lock = threading.Lock()
        self._timer = None
        self._down = False
        self._chorded = False
        self._press_sent = False
        self._pressed_at = 0

    def press(self) -> bool:
        """Observe key-down; return false only if safe arming was unavailable."""

        with self._lock:
            if self._down:
                return True
            self._down = True
            self._chorded = False
            self._press_sent = False
            self._pressed_at = self._monotonic_ns()
            try:
                timer = self._timer_factory(self._arm_delay, self._arm)
                if hasattr(timer, "daemon"):
                    timer.daemon = True
                self._timer = timer
                timer.start()
            except Exception:
                self._timer = None
                self._reset_locked()
                return False
            return True

    def other_key_pressed(self) -> bool:
        """Suppress an AltGr chord; return whether it emitted cancel."""

        with self._lock:
            if not self._down or self._chorded:
                return False
            self._chorded = True
            self._cancel_timer_locked()
            if self._press_sent:
                # The chord arrived after the arming window.  Clear daemon
                # ownership immediately instead of waiting for key-up.
                self._on_cancel()
                self._press_sent = False
                return True
            return False

    def release(self) -> None:
        """Observe key-up and forward it after any required press edge."""

        with self._lock:
            if not self._down:
                return
            released_at = self._monotonic_ns()
            self._cancel_timer_locked()
            if not self._chorded:
                if not self._press_sent:
                    self._on_press(self._pressed_at)
                self._on_release(released_at)
            self._reset_locked()

    def close(self) -> None:
        """Drop a not-yet-armed key; application shutdown sends cancel."""

        with self._lock:
            self._cancel_timer_locked()
            self._reset_locked()

    def _arm(self) -> None:
        with self._lock:
            self._timer = None
            if not self._down or self._chorded or self._press_sent:
                return
            self._press_sent = True
            # Keep the callback under the lock so a simultaneous release
            # cannot enqueue its edge ahead of this press.
            self._on_press(self._pressed_at)

    def _cancel_timer_locked(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

    def _reset_locked(self) -> None:
        self._down = False
        self._chorded = False
        self._press_sent = False
        self._pressed_at = 0
