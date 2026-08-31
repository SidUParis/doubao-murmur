"""Concurrency tests for the single FIFO daemon command worker."""

from __future__ import annotations

import threading
import time

from doubao_murmur.controller_worker import ControllerWorker
from doubao_murmur.daemon_control import DaemonControlError, DaemonReply


class _Controller:
    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []
        self.thread_ids: list[int] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_first = False

    def request(
        self, command: str, *, event_nanoseconds: int | None = None
    ) -> DaemonReply:
        self.calls.append(command)
        if event_nanoseconds is not None:
            self.calls[-1] = f"{command}:{event_nanoseconds}"
        self.thread_ids.append(threading.get_ident())
        if len(self.calls) == 1 and self.block_first:
            self.entered.set()
            assert self.release.wait(timeout=2)
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _worker(controller):
    completions = []
    event = threading.Event()

    def complete(*args):
        completions.append(args)
        event.set()

    worker = ControllerWorker(
        controller,
        completion=complete,
        post=lambda callback, *args: callback(*args),
    )
    return worker, completions, event


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_start_and_pending_stop_are_fifo_on_one_background_thread():
    controller = _Controller(
        [
            DaemonReply(True, "started", "starting"),
            DaemonReply(True, "stopping", "stopping"),
        ]
    )
    controller.block_first = True
    worker, completions, _ = _worker(controller)
    try:
        assert worker.submit_toggle("start")
        assert controller.entered.wait(timeout=1)
        assert worker.submit_toggle("stop")
        controller.release.set()
        _wait_for(lambda: len(completions) == 2)

        assert controller.calls == ["start", "stop"]
        assert len(set(controller.thread_ids)) == 1
        assert controller.thread_ids[0] != threading.get_ident()
        assert [item[0] for item in completions] == sorted(
            item[0] for item in completions
        )
    finally:
        worker.close()


def test_press_and_release_keep_fifo_order_and_physical_timestamps():
    controller = _Controller(
        [
            DaemonReply(True, "started", "starting"),
            DaemonReply(True, "stopping", "stopping"),
        ]
    )
    controller.block_first = True
    worker, completions, _ = _worker(controller)
    try:
        assert worker.submit_edge("press", 1_000_000_000)
        assert controller.entered.wait(timeout=1)
        assert worker.submit_edge("release", 2_000_000_000)
        controller.release.set()
        _wait_for(lambda: len(completions) == 2)

        assert controller.calls == [
            "press:1000000000",
            "release:2000000000",
        ]
    finally:
        worker.close()


def test_lost_press_reply_discards_release_and_cancels_uncertain_session():
    controller = _Controller(
        [
            DaemonControlError("request-timeout"),
            DaemonReply(True, "cancelled", "idle"),
        ]
    )
    controller.block_first = True
    worker, completions, _ = _worker(controller)
    try:
        worker.submit_edge("press", 1_000_000_000)
        assert controller.entered.wait(timeout=1)
        worker.submit_edge("release", 2_000_000_000)
        controller.release.set()
        _wait_for(lambda: len(completions) == 2)

        assert controller.calls == ["press:1000000000", "cancel"]
    finally:
        worker.close()


def test_failed_start_discards_pending_stop_instead_of_starting_again():
    controller = _Controller([DaemonReply(False, "preedit-rejected", "idle")])
    controller.block_first = True
    worker, completions, _ = _worker(controller)
    try:
        worker.submit_toggle("start")
        assert controller.entered.wait(timeout=1)
        worker.submit_toggle("stop")
        controller.release.set()
        _wait_for(lambda: len(completions) == 1)
        time.sleep(0.05)

        assert controller.calls == ["start"]
    finally:
        worker.close()


def test_stop_pressed_after_socket_reply_but_before_gtk_delivery_is_contingent():
    controller = _Controller([DaemonReply(False, "preedit-rejected", "idle")])
    posted = []
    completions = []
    worker = ControllerWorker(
        controller,
        completion=lambda *args: completions.append(args),
        post=lambda callback, *args: posted.append((callback, args)),
    )
    try:
        worker.submit_toggle("start")
        _wait_for(lambda: len(posted) == 1)

        # The socket request has returned, but GTK has not processed it yet;
        # the worker must still treat this as a pending stop, not a new start.
        worker.submit_toggle("stop")
        callback, args = posted.pop(0)
        callback(*args)
        _wait_for(lambda: len(completions) == 1)
        time.sleep(0.05)

        assert controller.calls == ["start"]
    finally:
        worker.close()


def test_uncertain_start_error_uses_cancel_and_never_retries_toggle():
    controller = _Controller(
        [
            DaemonControlError("request-timeout"),
            DaemonReply(True, "cancelled", "idle"),
        ]
    )
    controller.block_first = True
    worker, completions, _ = _worker(controller)
    try:
        worker.submit_toggle("start")
        assert controller.entered.wait(timeout=1)
        worker.submit_toggle("stop")
        controller.release.set()
        _wait_for(lambda: len(completions) == 2)

        assert controller.calls == ["start", "cancel"]
        assert controller.calls.count("start") == 1
    finally:
        worker.close()


def test_escape_replaces_unsent_toggle_with_cancel():
    controller = _Controller(
        [
            DaemonReply(True, "status", "idle"),
            DaemonReply(False, "no-active-session", "idle"),
        ]
    )
    controller.block_first = True
    worker, completions, _ = _worker(controller)
    try:
        worker.submit_status()
        assert controller.entered.wait(timeout=1)
        worker.submit_toggle("start")
        worker.submit_cancel()
        controller.release.set()
        _wait_for(lambda: len(completions) == 2)

        assert controller.calls == ["status", "cancel"]
    finally:
        worker.close()


def test_escape_overrides_pending_stop_after_inflight_start():
    controller = _Controller(
        [
            DaemonReply(True, "started", "starting"),
            DaemonReply(True, "cancelled", "idle"),
        ]
    )
    controller.block_first = True
    worker, completions, _ = _worker(controller)
    try:
        worker.submit_toggle("start")
        assert controller.entered.wait(timeout=1)
        worker.submit_toggle("stop")
        worker.submit_cancel()
        controller.release.set()
        _wait_for(lambda: len(completions) == 2)

        assert controller.calls == ["start", "cancel"]
    finally:
        worker.close()


def test_duplicate_status_is_coalesced():
    controller = _Controller([DaemonReply(True, "status", "recording")])
    controller.block_first = True
    worker, completions, _ = _worker(controller)
    try:
        assert worker.submit_status()
        assert controller.entered.wait(timeout=1)
        assert not worker.submit_status()
        controller.release.set()
        _wait_for(lambda: len(completions) == 1)

        assert controller.calls == ["status"]
    finally:
        worker.close()


def test_lone_uncertain_start_error_is_reconciled_with_cancel():
    controller = _Controller(
        [
            DaemonControlError("request-timeout"),
            DaemonReply(True, "cancelled", "idle"),
        ]
    )
    worker, completions, _ = _worker(controller)
    try:
        worker.submit_toggle("start")
        _wait_for(lambda: len(completions) == 2)

        assert controller.calls == ["start", "cancel"]
    finally:
        worker.close()


def test_explicit_stop_never_turns_an_idle_daemon_back_on():
    controller = _Controller([DaemonReply(False, "no-active-session", "idle")])
    worker, completions, _ = _worker(controller)
    try:
        worker.submit_toggle("stop")
        _wait_for(lambda: len(completions) == 1)

        assert controller.calls == ["stop"]
    finally:
        worker.close()


def test_observation_restart_is_the_only_action_that_uses_toggle():
    controller = _Controller([DaemonReply(True, "started", "starting")])
    worker, completions, _ = _worker(controller)
    try:
        worker.submit_toggle("restart")
        _wait_for(lambda: len(completions) == 1)

        assert controller.calls == ["toggle"]
    finally:
        worker.close()


def test_stop_during_observation_restart_is_contingent_and_explicit():
    controller = _Controller(
        [
            DaemonReply(True, "started", "starting"),
            DaemonReply(True, "stopping", "stopping"),
        ]
    )
    controller.block_first = True
    worker, completions, _ = _worker(controller)
    try:
        worker.submit_toggle("restart")
        assert controller.entered.wait(timeout=1)
        worker.submit_toggle("stop")
        controller.release.set()
        _wait_for(lambda: len(completions) == 2)

        assert controller.calls == ["toggle", "stop"]
    finally:
        worker.close()


def test_close_drops_completion_from_an_inflight_request():
    controller = _Controller([DaemonReply(True, "status", "idle")])
    controller.block_first = True
    worker, completions, _ = _worker(controller)

    worker.submit_status()
    assert controller.entered.wait(timeout=1)
    worker.close()
    controller.release.set()
    time.sleep(0.05)

    assert controller.calls == ["status"]
    assert completions == []


def test_close_drops_a_completion_already_posted_to_gtk():
    controller = _Controller([DaemonReply(True, "status", "idle")])
    posted = []
    completions = []
    worker = ControllerWorker(
        controller,
        completion=lambda *args: completions.append(args),
        post=lambda callback, *args: posted.append((callback, args)),
    )
    try:
        worker.submit_status()
        _wait_for(lambda: len(posted) == 1)
        worker.close()
        callback, args = posted.pop()
        callback(*args)

        assert completions == []
    finally:
        worker.close()
