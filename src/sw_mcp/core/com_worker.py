"""A single STA thread that owns every SolidWorks COM call.

COM objects from SolidWorks must be used on the apartment that created them, so all tool
bodies are queued here and run one at a time. If a call hangs (typically a modal dialog
in SolidWorks), the lane is abandoned after `stuck_after` seconds and a fresh thread with
a fresh connection takes over; the old thread exits once SolidWorks lets go of it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable

import pythoncom

from .errors import Code, SwError

log = logging.getLogger(__name__)


class _Lane:
    def __init__(self, gen: int, cleanup: Callable[[], None] | None) -> None:
        self.gen = gen
        self.queue: queue.Queue = queue.Queue()
        self.busy_since: float | None = None
        self._cleanup = cleanup
        self.thread = threading.Thread(target=self._loop, name=f"sw-com-{gen}", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        pythoncom.CoInitialize()  # single-threaded apartment
        try:
            while True:
                try:
                    item = self.queue.get(timeout=0.05)
                except queue.Empty:
                    pythoncom.PumpWaitingMessages()
                    continue
                if item is None:
                    return
                fn, fut = item
                if not fut.set_running_or_notify_cancel():
                    continue  # caller already gave up; never run stale work
                self.busy_since = time.monotonic()
                try:
                    fut.set_result(fn())
                except BaseException as exc:  # noqa: BLE001 - delivered to the caller
                    fut.set_exception(exc)
                finally:
                    self.busy_since = None
                    pythoncom.PumpWaitingMessages()
        finally:
            if self._cleanup is not None:
                try:
                    self._cleanup()
                except Exception:  # noqa: BLE001
                    log.debug("lane cleanup failed", exc_info=True)
            pythoncom.CoUninitialize()


class ComWorker:
    def __init__(self, stuck_after: float = 30.0, cleanup: Callable[[], None] | None = None) -> None:
        self.stuck_after = stuck_after
        self._cleanup = cleanup
        self._lock = threading.Lock()
        self._lane: _Lane | None = None
        self._gen = 0

    @property
    def generation(self) -> int:
        return self._gen

    def _new_lane_locked(self) -> _Lane:
        if self._lane is not None:
            self._lane.queue.put(None)  # let the old thread exit when it is free
        self._gen += 1
        self._lane = _Lane(self._gen, self._cleanup)
        return self._lane

    def _lane_for_call(self, fresh_if_busy: bool) -> _Lane:
        with self._lock:
            lane = self._lane
            if lane is None or not lane.thread.is_alive():
                return self._new_lane_locked()
            busy = lane.busy_since
            if busy is not None:
                stuck_for = time.monotonic() - busy
                if stuck_for > self.stuck_after or fresh_if_busy:
                    log.warning("COM lane %d busy for %.1fs; starting a fresh lane", lane.gen, stuck_for)
                    return self._new_lane_locked()
            return lane

    def submit(self, fn: Callable[[], Any], timeout: float, fresh_if_busy: bool = False) -> Any:
        """Run `fn` on the COM thread and wait up to `timeout` seconds for its result."""
        lane = self._lane_for_call(fresh_if_busy)
        fut: Future = Future()
        lane.queue.put((fn, fut))
        try:
            return fut.result(timeout)
        except FutureTimeout:
            if fut.cancel():
                raise SwError(
                    Code.TIMEOUT,
                    f"The request waited {timeout:.0f}s behind another SolidWorks call and was cancelled. Nothing was changed.",
                    "Call get_status, then try again.",
                    infra=True,
                ) from None
            raise SwError(
                Code.TIMEOUT,
                f"SolidWorks did not answer within {timeout:.0f}s. It may be showing a dialog box or doing a long rebuild. "
                "The operation may still finish on its own.",
                "Ask the user to look at the SolidWorks window and close any dialog. Then call get_status "
                "and check the result before repeating the action.",
                infra=True,
            ) from None

    def shutdown(self) -> None:
        with self._lock:
            if self._lane is not None:
                self._lane.queue.put(None)
                self._lane = None
