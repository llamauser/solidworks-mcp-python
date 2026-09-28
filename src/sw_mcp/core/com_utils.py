"""Helpers that make COM calls behave the same under every pywin32 binding mode.

pywin32 exposes a zero-argument SolidWorks method either as a bound method (when it has
type info) or as an already-evaluated value (when it does not). `call()` hides that
difference, so tool code never has to guess whether `GetTitle` needs parentheses.
"""

from __future__ import annotations

import math
from functools import partial
from types import BuiltinMethodType, FunctionType, MethodType
from typing import Any

import pythoncom
import pywintypes
import win32com.client

_FUNCTION_TYPES = (MethodType, FunctionType, BuiltinMethodType, partial)


def call(obj: Any, name: str, *args: Any) -> Any:
    """Call method `name` or read property `name`, whichever pywin32 hands back."""
    attr = getattr(obj, name)
    # COM results (CDispatch) are callable too, so test for real Python functions only.
    if isinstance(attr, _FUNCTION_TYPES):
        return attr(*args)
    if args:
        raise TypeError(f"{name} is a property here, it cannot take arguments")
    return attr


def try_call(obj: Any, name: str, *args: Any, default: Any = None) -> Any:
    """Like call(), but returns `default` when the member is missing or the call fails."""
    if obj is None:
        return default
    try:
        return call(obj, name, *args)
    except (pywintypes.com_error, AttributeError, TypeError):
        return default


def first_ok(*attempts: tuple) -> Any:
    """Try (obj, name, *args) tuples in order: newest API first, older fallbacks after.

    Returns the first result that did not raise. Re-raises the last error if all fail.
    """
    last: BaseException | None = None
    for obj, name, *args in attempts:
        try:
            return call(obj, name, *args)
        except (pywintypes.com_error, AttributeError, TypeError) as exc:
            last = exc
    if last is not None:
        raise last
    raise ValueError("first_ok() needs at least one attempt")


def null_dispatch() -> Any:
    """A typed 'Nothing' for optional object arguments (e.g. SelectByID2's Callout).

    Passing plain None sends VT_EMPTY, which SolidWorks rejects with a type mismatch.
    """
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def byref_int() -> Any:
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)


def call_with_out_ints(obj: Any, name: str, *args: Any, n_out: int = 2) -> tuple[Any, list[int]]:
    """Call a method whose last `n_out` arguments are ByRef longs (errors/warnings).

    Depending on binding, pywin32 either returns (result, out1, out2) or updates the
    VARIANTs in place. Both are handled.
    """
    outs = [byref_int() for _ in range(n_out)]
    result = call(obj, name, *args, *outs)
    if isinstance(result, tuple) and len(result) == n_out + 1:
        return result[0], [int(v or 0) for v in result[1:]]
    return result, [int(o.value or 0) for o in outs]


def as_list(value: Any) -> list:
    """Normalize a COM array result (None, tuple, list or single object) to a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


# ---- units: SolidWorks API works in meters and radians; tools speak mm and degrees ----

def _clean(x: float, nd: int) -> float:
    v = round(float(x), nd)
    return 0.0 if v == 0 else v  # no "-0.0"


def mm(meters: Any, nd: int = 3) -> float | None:
    return None if meters is None else _clean(float(meters) * 1000.0, nd)


def mm2(square_meters: Any, nd: int = 2) -> float | None:
    return None if square_meters is None else _clean(float(square_meters) * 1e6, nd)


def deg(radians: Any, nd: int = 3) -> float | None:
    return None if radians is None else _clean(math.degrees(float(radians)), nd)


def vec(values: Any, nd: int = 4) -> list[float] | None:
    vals = as_list(values)
    if len(vals) < 3:
        return None
    return [_clean(v, nd) for v in vals[:3]]


def point_mm(values: Any, nd: int = 3) -> list[float] | None:
    vals = as_list(values)
    if len(vals) < 3:
        return None
    return [mm(v, nd) for v in vals[:3]]
