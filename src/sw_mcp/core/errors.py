"""The one exception type tools raise internally. It always carries a fix hint for the LLM."""

from __future__ import annotations


class Code:
    NO_SOLIDWORKS = "NO_SOLIDWORKS"
    SW_STARTING = "SW_STARTING"
    SW_BUSY = "SW_BUSY"
    SW_DISCONNECTED = "SW_DISCONNECTED"
    TIMEOUT = "TIMEOUT"
    BREAKER_OPEN = "SW_UNAVAILABLE"
    NO_ACTIVE_DOC = "NO_ACTIVE_DOC"
    WRONG_DOC_TYPE = "WRONG_DOC_TYPE"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_EXISTS = "FILE_EXISTS"
    BAD_ARGUMENT = "BAD_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    READ_ONLY = "READ_ONLY"
    OPEN_FAILED = "OPEN_FAILED"
    SAVE_FAILED = "SAVE_FAILED"
    REBUILD_FAILED = "REBUILD_FAILED"
    SW_ERROR = "SW_ERROR"
    UNSUPPORTED = "UNSUPPORTED_BY_THIS_SOLIDWORKS"
    COM_ERROR = "COM_ERROR"
    INTERNAL = "INTERNAL_ERROR"


class SwError(Exception):
    """A failure explained in plain words.

    infra:     the failure is about the connection to SolidWorks (not the LLM's arguments);
               only these count toward the circuit breaker.
    retryable: safe to retry the same call after a short wait (SolidWorks was busy).
    reconnect: the cached COM handle is dead and must be dropped.
    """

    def __init__(
        self,
        code: str,
        message: str,
        fix: str = "",
        *,
        infra: bool = False,
        retryable: bool = False,
        reconnect: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.infra = infra
        self.retryable = retryable
        self.reconnect = reconnect

    def to_dict(self) -> dict:
        out = {"ok": False, "error": self.code, "message": self.message}
        if self.fix:
            out["fix"] = self.fix
        return out
