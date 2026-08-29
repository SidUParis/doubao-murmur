"""Single-threaded command queue for the controller-only GTK sidecar."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from doubao_murmur.daemon_control import (
    DaemonController,
    DaemonControlError,
    DaemonReply,
)


@dataclass(frozen=True, slots=True)
class _WorkItem:
    sequence: int
    command: str
    intent: str = ""


Completion = Callable[[int, str, DaemonReply | None, str | None], None]
Poster = Callable[..., object]


class ControllerWorker:
    """Serialize socket requests without ever blocking GTK.

    Normal actions use the daemon's explicit start/stop commands. The one
    non-idempotent toggle is reserved for observation-to-next-dictation and is
    never retried. If the user presses stop while a start-like action is in
    flight, one pending stop is remembered and issued only after its reply
    proves that a session is active. Cancel discards every unsent action/status
    and is always queued next.
    """

    def __init__(
        self,
        controller: DaemonController,
        *,
        completion: Completion,
        post: Poster,
    ) -> None:
        self._controller = controller
        self._completion = completion
        self._post = post
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._queue: deque[_WorkItem] = deque()
        self._sequence = 0
        self._inflight: _WorkItem | None = None
        self._queued_action: _WorkItem | None = None
        self._pending_stop = False
        self._status_pending = False
        self._delivery_event: threading.Event | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="openvoice-controller",
            daemon=True,
        )
        self._thread.start()

    def submit_toggle(self, intent: str) -> int | None:
        if intent not in {"start", "stop", "restart"}:
            return None
        command = {"start": "start", "stop": "stop", "restart": "toggle"}[intent]
        with self._lock:
            if self._closed:
                return None
            existing = self._queued_action
            if existing is None and self._inflight is not None:
                if self._inflight.intent in {"start", "stop", "restart"}:
                    existing = self._inflight
            if existing is not None:
                if existing.intent in {"start", "restart"} and intent == "stop":
                    self._pending_stop = True
                    return existing.sequence
                return None
            item = self._new_item_locked(command, intent)
            self._queued_action = item
            self._queue.append(item)
            self._condition.notify()
            return item.sequence

    def submit_cancel(self) -> int | None:
        with self._lock:
            if self._closed:
                return None
            self._pending_stop = False
            self._drain_unsent_locked()
            item = self._new_item_locked("cancel")
            self._queue.append(item)
            self._condition.notify()
            return item.sequence

    def submit_status(self) -> int | None:
        with self._lock:
            if self._closed or self._status_pending:
                return None
            if self._inflight is not None or self._queued_action is not None:
                return None
            item = self._new_item_locked("status")
            self._status_pending = True
            self._queue.append(item)
            self._condition.notify()
            return item.sequence

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending_stop = False
            self._drain_unsent_locked()
            if self._delivery_event is not None:
                self._delivery_event.set()
            self._condition.notify()

    def _new_item_locked(self, command: str, intent: str = "") -> _WorkItem:
        self._sequence += 1
        return _WorkItem(self._sequence, command, intent)

    def _drain_unsent_locked(self) -> None:
        while self._queue:
            item = self._queue.popleft()
            if (
                item.intent in {"start", "stop", "restart"}
                and item is self._queued_action
            ):
                self._queued_action = None
            if item.command == "status":
                self._status_pending = False

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if self._closed and not self._queue:
                    return
                item = self._queue.popleft()
                self._inflight = item
                if (
                    item.intent in {"start", "stop", "restart"}
                    and item is self._queued_action
                ):
                    self._queued_action = None

            reply: DaemonReply | None = None
            error_code: str | None = None
            try:
                reply = self._controller.request(item.command)
            except DaemonControlError as error:
                error_code = error.code
            except Exception:
                error_code = "invalid-response"

            delivered = threading.Event()
            with self._lock:
                if self._closed:
                    self._inflight = None
                    return
                self._delivery_event = delivered
            self._post(
                self._deliver_completion,
                delivered,
                item,
                item.sequence,
                item.command,
                reply,
                error_code,
            )
            delivered.wait()
            with self._lock:
                if self._delivery_event is delivered:
                    self._delivery_event = None

    def _deliver_completion(
        self,
        delivered: threading.Event,
        item: _WorkItem,
        sequence: int,
        command: str,
        reply: DaemonReply | None,
        error_code: str | None,
    ) -> object:
        with self._lock:
            if self._closed:
                self._inflight = None
                if item.command == "status":
                    self._status_pending = False
                delivered.set()
                return False
        try:
            return self._completion(sequence, command, reply, error_code)
        finally:
            with self._lock:
                self._inflight = None
                if item.command == "status":
                    self._status_pending = False
                if item.intent in {"start", "restart"}:
                    self._queue_pending_stop_locked(reply, error_code)
            delivered.set()

    def _queue_pending_stop_locked(
        self,
        reply: DaemonReply | None,
        error_code: str | None,
    ) -> None:
        pending_stop = self._pending_stop
        self._pending_stop = False
        if self._closed:
            return
        if error_code is not None:
            # A start whose reply was lost has an uncertain outcome. Cancel is
            # safe whether it eventually started or failed before acquisition.
            self._queue.append(self._new_item_locked("cancel"))
            self._condition.notify()
            return
        if not pending_stop:
            return
        if (
            reply is not None
            and reply.ok
            and reply.state in {"starting", "recording", "observing"}
        ):
            stop = self._new_item_locked("stop", "stop")
            self._queued_action = stop
            self._queue.append(stop)
            self._condition.notify()
