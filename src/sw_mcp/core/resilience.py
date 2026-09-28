"""Error classification, circuit breaker and the @sw_tool wrapper.

Adapted from the TypeScript reference's circuit breaker. The TS "complexity analyzer"
existed to dodge a Node COM bridge limit on argument counts; pywin32 has no such limit,
so that role becomes pre-flight checks (`needs=`) plus error classification here.

Every tool built with @sw_tool:
- runs its body on the COM worker thread,
- never raises: the LLM always receives a JSON string with ok true/false,
- only lets connection-level failures (not bad arguments) trip the circuit breaker,
- retries only when marked idempotent and SolidWorks said it was busy.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import threading
import time
from typing import Any, Callable

import pywintypes

from .. import config
from . import connection
from .com_utils import call, try_call
from .com_worker import ComWorker
from .errors import Code, SwError

log = logging.getLogger("sw_mcp.tools")

# ---------------------------------------------------------------- HRESULTs
RPC_E_CALL_REJECTED = 0x80010001
RPC_E_SERVERCALL_RETRYLATER = 0x8001010A
RPC_E_DISCONNECTED = 0x80010108
RPC_E_SERVER_DIED = 0x80010007
RPC_E_SERVER_DIED_DNE = 0x80010012
RPC_S_SERVER_UNAVAILABLE = 0x800706BA
RPC_S_CALL_FAILED = 0x800706BE
RPC_S_CALL_FAILED_DNE = 0x800706BF
CO_E_OBJNOTCONNECTED = 0x800401FD
MK_E_UNAVAILABLE = 0x800401E3
DISP_E_EXCEPTION = 0x80020009
DISP_E_MEMBERNOTFOUND = 0x80020003
DISP_E_PARAMNOTFOUND = 0x80020004
DISP_E_TYPEMISMATCH = 0x80020005
DISP_E_UNKNOWNNAME = 0x80020006
DISP_E_BADPARAMCOUNT = 0x8002000E

DISCONNECTED = {
    RPC_E_DISCONNECTED, RPC_E_SERVER_DIED, RPC_E_SERVER_DIED_DNE, RPC_S_SERVER_UNAVAILABLE,
    RPC_S_CALL_FAILED, RPC_S_CALL_FAILED_DNE, CO_E_OBJNOTCONNECTED,
}
BUSY = {RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER}
API_MISMATCH = {
    DISP_E_MEMBERNOTFOUND, DISP_E_PARAMNOTFOUND, DISP_E_TYPEMISMATCH,
    DISP_E_UNKNOWNNAME, DISP_E_BADPARAMCOUNT,
}


def hresult(exc: pywintypes.com_error) -> int:
    return int(exc.hresult) & 0xFFFFFFFF


def _com_description(exc: pywintypes.com_error) -> str:
    info = exc.excepinfo
    if info and len(info) > 2 and info[2]:
        return str(info[2]).strip()
    return str(exc.strerror or "").strip()


def classify(exc: BaseException) -> SwError:
    """Turn any exception into an SwError with a plain-language message and a fix hint."""
    if isinstance(exc, SwError):
        return exc
    if isinstance(exc, pywintypes.com_error):
        hr = hresult(exc)
        if hr in DISCONNECTED:
            return SwError(
                Code.SW_DISCONNECTED,
                "Lost the connection to SolidWorks (it was closed or crashed).",
                "Call get_status to reconnect.",
                infra=True, reconnect=True,
            )
        if hr in BUSY:
            return SwError(
                Code.SW_BUSY,
                "SolidWorks is busy and refused the request (usually a dialog box is open).",
                "Ask the user to close any open dialog in SolidWorks, then try again.",
                infra=True, retryable=True,
            )
        if hr == MK_E_UNAVAILABLE:
            return SwError(Code.NO_SOLIDWORKS, "SolidWorks is not running.", "Call get_status to start it.")
        if hr == DISP_E_EXCEPTION:
            return SwError(
                Code.SW_ERROR,
                f"SolidWorks reported an error: {_com_description(exc) or 'no details'}.",
                "Check the arguments. Call get_status if unsure what state SolidWorks is in.",
            )
        if hr in API_MISMATCH:
            return SwError(
                Code.UNSUPPORTED,
                f"This SolidWorks version does not accept a call this tool makes (0x{hr:08X}).",
                "Tell the user this tool is not supported on their SolidWorks version. Try a different tool.",
            )
        return SwError(
            Code.COM_ERROR,
            f"SolidWorks COM error 0x{hr:08X}: {_com_description(exc) or 'no details'}.",
            "Call get_status, then try again once. If it fails again, tell the user.",
        )
    if isinstance(exc, AttributeError) and "NoneType" not in str(exc):
        return SwError(
            Code.UNSUPPORTED,
            f"This SolidWorks version is missing an API member this tool uses ({exc}).",
            "Tell the user this tool is not supported on their SolidWorks version.",
        )
    return SwError(
        Code.INTERNAL,
        f"Internal error in the tool: {type(exc).__name__}: {exc}",
        "Do not repeat the same call. Tell the user what you were trying to do.",
    )


# ---------------------------------------------------------------- circuit breaker
class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, threshold: int, cooldown: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = max(1, threshold)
        self.cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self.state = self.CLOSED
        self.failures = 0
        self._opened_at = 0.0
        self._trial_running = False

    def before_call(self) -> None:
        with self._lock:
            if self.state == self.OPEN:
                wait = self.cooldown - (self._clock() - self._opened_at)
                if wait > 0:
                    raise SwError(
                        Code.BREAKER_OPEN,
                        f"SolidWorks stopped responding to recent requests, so calls are paused for {wait:.0f} more seconds.",
                        "Ask the user to check the SolidWorks window for an open dialog or a frozen screen. "
                        "Then call get_status, which reconnects immediately.",
                    )
                self.state = self.HALF_OPEN
                self._trial_running = False
            if self.state == self.HALF_OPEN:
                if self._trial_running:
                    raise SwError(
                        Code.BREAKER_OPEN,
                        "SolidWorks is being re-tested after recent failures.",
                        "Wait a few seconds, then call get_status.",
                    )
                self._trial_running = True

    def record_success(self) -> None:
        with self._lock:
            self.state = self.CLOSED
            self.failures = 0
            self._trial_running = False

    def record_failure(self, err: SwError) -> None:
        with self._lock:
            self._trial_running = False
            if not err.infra:
                if self.state == self.HALF_OPEN:  # SolidWorks answered: it is reachable
                    self.state = self.CLOSED
                    self.failures = 0
                return
            self.failures += self.threshold if err.code == Code.TIMEOUT else 1
            if self.state == self.HALF_OPEN or self.failures >= self.threshold:
                self.state = self.OPEN
                self._opened_at = self._clock()

    def reset(self) -> None:
        self.record_success()

    def snapshot(self) -> dict:
        return {"state": self.state, "recent_failures": self.failures}


# ---------------------------------------------------------------- runtime singletons
class Runtime:
    def __init__(self) -> None:
        self.worker = ComWorker(stuck_after=config.COM_TIMEOUT_S, cleanup=connection.reset)
        self.breaker = CircuitBreaker(config.BREAKER_THRESHOLD, config.BREAKER_COOLDOWN_S)


runtime = Runtime()


# ---------------------------------------------------------------- per-call session
DOC_TYPES = {1: "part", 2: "assembly", 3: "drawing"}
_NEEDS_TYPES = {
    "part": {1}, "assembly": {2}, "drawing": {3}, "model": {1, 2}, "doc": {1, 2, 3},
}


class Session:
    """What a tool body receives: lazy access to the app and the validated active document."""

    def __init__(self, needs: str | None) -> None:
        self._app: Any = None
        self._doc: Any = None
        if needs is None:
            return
        self.app  # noqa: B018 - attach now so failures are reported before the tool runs
        if needs in _NEEDS_TYPES:
            self.require(needs)

    @property
    def app(self) -> Any:
        if self._app is None:
            self._app = connection.get_app(allow_launch=False)
        return self._app

    def connect(self, allow_launch: bool) -> Any:
        self._app = connection.get_app(allow_launch=allow_launch)
        return self._app

    @property
    def doc(self) -> Any:
        if self._doc is None:
            doc = connection.active_doc(self.app)
            if doc is None:
                raise SwError(
                    Code.NO_ACTIVE_DOC,
                    "No document is open in SolidWorks.",
                    "Call open_document with a full file path, or ask the user to open a file.",
                )
            self._doc = doc
        return self._doc

    def doc_type(self) -> str:
        return DOC_TYPES.get(try_call(self.doc, "GetType", default=0), "unknown")

    def require(self, needs: str) -> None:
        allowed = _NEEDS_TYPES[needs]
        kind = try_call(self.doc, "GetType", default=0)
        if kind not in allowed:
            want = " or ".join(DOC_TYPES[k] for k in sorted(allowed))
            have = DOC_TYPES.get(kind, "unknown")
            raise SwError(
                Code.WRONG_DOC_TYPE,
                f"This tool needs a {want} document, but the active document is a {have}.",
                "Call open_document to open the right file, or ask the user to switch windows.",
            )


# ---------------------------------------------------------------- the decorator
def to_json(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)


def _short(kwargs: dict) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    return text if len(text) < 300 else text[:297] + "..."


def sw_tool(
    *,
    needs: str | None = "app",
    idempotent: bool = False,
    recovery: bool = False,
    timeout: float | None = None,
) -> Callable[[Callable[..., dict]], Callable[..., str]]:
    """Wrap `fn(sw: Session, **args) -> dict` into an MCP tool that returns a JSON string.

    needs:      None | "app" | "doc" | "part" | "assembly" | "drawing" | "model" (part or assembly)
    idempotent: safe to repeat automatically when SolidWorks says it is busy
    recovery:   bypasses the circuit breaker and closes it on success (get_status)
    """

    def decorate(fn: Callable[..., dict]) -> Callable[..., str]:
        sig = inspect.signature(fn, eval_str=True)
        public = list(sig.parameters.values())[1:]  # drop `sw`

        def job(args: tuple, kwargs: dict) -> dict:
            try:
                return fn(Session(needs), *args, **kwargs)
            except BaseException as exc:
                err = classify(exc)
                if err.reconnect:
                    connection.reset()
                if err is not exc:
                    log.debug("%s raised", fn.__name__, exc_info=True)
                raise err from None

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            started = time.monotonic()
            attempts = 1 + (config.BUSY_RETRIES if idempotent else 0)
            try:
                if not recovery:
                    runtime.breaker.before_call()
                for attempt in range(attempts):
                    try:
                        data = runtime.worker.submit(
                            lambda: job(args, kwargs),
                            timeout or config.COM_TIMEOUT_S,
                            fresh_if_busy=recovery and runtime.breaker.state != CircuitBreaker.CLOSED,
                        )
                        break
                    except SwError as err:
                        if err.retryable and attempt + 1 < attempts:
                            time.sleep(config.BUSY_RETRY_DELAY_S * (attempt + 1))
                            continue
                        raise
                runtime.breaker.record_success()
                result = {"ok": True, **(data or {})}
                log.info("%s(%s) ok in %.0f ms", fn.__name__, _short(kwargs), (time.monotonic() - started) * 1000)
            except BaseException as exc:  # noqa: BLE001 - the server must never crash
                err = classify(exc)
                if recovery and not err.infra:
                    runtime.breaker.reset()  # SolidWorks answered, even if with "not running"
                else:
                    runtime.breaker.record_failure(err)
                result = err.to_dict()
                log.warning(
                    "%s(%s) failed in %.0f ms: %s %s",
                    fn.__name__, _short(kwargs), (time.monotonic() - started) * 1000, err.code, err.message,
                )
            return to_json(result)

        wrapper.__signature__ = sig.replace(parameters=public, return_annotation=str)  # type: ignore[attr-defined]
        wrapper.__annotations__ = {p.name: p.annotation for p in public} | {"return": str}
        wrapper.body = fn  # type: ignore[attr-defined] - for tests
        return wrapper

    return decorate


__all__ = ["CircuitBreaker", "Runtime", "Session", "classify", "runtime", "sw_tool", "to_json", "call"]
