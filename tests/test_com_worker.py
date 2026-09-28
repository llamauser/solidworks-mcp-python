from __future__ import annotations

import threading
import time

import pytest

from sw_mcp.core.com_worker import ComWorker
from sw_mcp.core.errors import Code, SwError


def test_all_jobs_run_on_one_com_thread():
    w = ComWorker()
    try:
        names = {w.submit(lambda: threading.current_thread().name, 5) for _ in range(5)}
        assert len(names) == 1 and next(iter(names)).startswith("sw-com-")
        assert threading.current_thread().name not in names
    finally:
        w.shutdown()


def test_com_is_initialized_on_worker():
    import pythoncom

    w = ComWorker()
    try:
        # CoInitialize a second time returns S_FALSE (1) when already initialized: no error.
        assert w.submit(lambda: pythoncom.CoInitialize() or "ok", 5) == "ok"
    finally:
        w.shutdown()


def test_exceptions_reach_the_caller():
    w = ComWorker()
    try:
        with pytest.raises(ZeroDivisionError):
            w.submit(lambda: 1 / 0, 5)
        assert w.submit(lambda: 42, 5) == 42  # the lane survives
    finally:
        w.shutdown()


def test_timeout_and_queued_job_is_never_run_late():
    w = ComWorker(stuck_after=60)
    ran = []
    release = threading.Event()
    try:
        t = threading.Thread(target=lambda: w.submit(lambda: release.wait(5), 10))
        t.start()
        time.sleep(0.2)
        with pytest.raises(SwError) as info:
            w.submit(lambda: ran.append("late"), 0.3)  # waits behind the blocked job
        assert info.value.code == Code.TIMEOUT and info.value.infra
        release.set()
        t.join()
        assert w.submit(lambda: "after", 5) == "after"
        assert ran == []  # the cancelled job was skipped, not executed afterwards
    finally:
        release.set()
        w.shutdown()


def test_stuck_lane_is_replaced():
    w = ComWorker(stuck_after=0.2)
    release = threading.Event()
    try:
        with pytest.raises(SwError):
            w.submit(lambda: release.wait(5), 0.3)
        gen = w.generation
        assert w.submit(lambda: "fresh", 5) == "fresh"
        assert w.generation == gen + 1
    finally:
        release.set()
        w.shutdown()
