from __future__ import annotations

import pytest
import pywintypes

from sw_mcp.core.errors import Code, SwError
from sw_mcp.core.resilience import CircuitBreaker, classify, runtime, sw_tool
from tests.conftest import parse


def com_error(hr: int, desc: str = "") -> pywintypes.com_error:
    excepinfo = (0, "SolidWorks", desc, None, 0, hr - 2**32) if desc else None
    return pywintypes.com_error(hr - 2**32, "msg", excepinfo, None)


@pytest.mark.parametrize(
    "hr, code, infra",
    [
        (0x80010108, Code.SW_DISCONNECTED, True),
        (0x800706BA, Code.SW_DISCONNECTED, True),
        (0x80010001, Code.SW_BUSY, True),
        (0x8001010A, Code.SW_BUSY, True),
        (0x80020005, Code.UNSUPPORTED, False),
        (0x80020009, Code.SW_ERROR, False),
        (0x80004005, Code.COM_ERROR, False),
    ],
)
def test_classify_hresults(hr, code, infra):
    err = classify(com_error(hr, "boom"))
    assert err.code == code and err.infra == infra and err.fix


def test_classify_python_errors():
    assert classify(ValueError("x")).code == Code.INTERNAL
    assert classify(AttributeError("<unknown>.FeatureExtrusion4")).code == Code.UNSUPPORTED
    assert classify(AttributeError("'NoneType' object has no attribute 'x'")).code == Code.INTERNAL


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def infra_error():
    return SwError(Code.SW_BUSY, "busy", infra=True)


def test_breaker_ignores_argument_mistakes():
    b = CircuitBreaker(threshold=2, cooldown=10)
    for _ in range(10):
        b.before_call()
        b.record_failure(SwError(Code.NOT_FOUND, "no such dimension"))
    assert b.state == "closed"


def test_breaker_opens_and_recovers():
    clock = Clock()
    b = CircuitBreaker(threshold=2, cooldown=10, clock=clock)
    b.record_failure(infra_error())
    b.record_failure(infra_error())
    assert b.state == "open"
    with pytest.raises(SwError) as info:
        b.before_call()
    assert info.value.code == Code.BREAKER_OPEN
    clock.t = 11
    b.before_call()  # trial call allowed
    assert b.state == "half_open"
    with pytest.raises(SwError):
        b.before_call()  # only one trial at a time
    b.record_success()
    assert b.state == "closed"


def test_timeout_opens_breaker_immediately():
    b = CircuitBreaker(threshold=3, cooldown=10)
    b.record_failure(SwError(Code.TIMEOUT, "t", infra=True))
    assert b.state == "open"


def test_half_open_failure_reopens():
    clock = Clock()
    b = CircuitBreaker(threshold=1, cooldown=5, clock=clock)
    b.record_failure(infra_error())
    clock.t = 6
    b.before_call()
    b.record_failure(infra_error())
    assert b.state == "open"


# ---------------------------------------------------------------- the decorator
@sw_tool(needs=None)
def crashing_tool(sw, x: int) -> dict:
    return {"value": 1 / x}


def test_tool_never_raises():
    assert parse(crashing_tool(x=2)) == {"ok": True, "value": 0.5}
    out = parse(crashing_tool(x=0))
    assert out["ok"] is False and out["error"] == Code.INTERNAL and "fix" in out


def test_tool_signature_hides_session():
    import inspect

    params = list(inspect.signature(crashing_tool).parameters)
    assert params == ["x"]


calls = {"n": 0}


@sw_tool(needs=None, idempotent=True)
def flaky_busy_tool(sw) -> dict:
    calls["n"] += 1
    if calls["n"] < 3:
        raise com_error(0x80010001)
    return {"attempts": calls["n"]}


def test_idempotent_tool_retries_when_busy(monkeypatch):
    from sw_mcp import config

    monkeypatch.setattr(config, "BUSY_RETRY_DELAY_S", 0.01)
    calls["n"] = 0
    assert parse(flaky_busy_tool()) == {"ok": True, "attempts": 3}
    assert runtime.breaker.state == "closed"


@sw_tool(needs=None)
def disconnected_tool(sw) -> dict:
    raise com_error(0x80010108)


def test_repeated_disconnects_open_the_breaker_and_fail_fast():
    for _ in range(runtime.breaker.threshold):
        assert parse(disconnected_tool())["error"] == Code.SW_DISCONNECTED
    assert runtime.breaker.state == "open"
    assert parse(disconnected_tool())["error"] == Code.BREAKER_OPEN
