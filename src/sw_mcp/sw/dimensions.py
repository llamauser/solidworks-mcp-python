"""Find and change a dimension safely: verify after rebuild, roll back on failure."""

from __future__ import annotations

import math
from typing import Any

from ..core.com_utils import call, try_call
from ..core.errors import Code, SwError
from . import constants as C
from .geometry import dimension_name, dimension_value


def _candidates(name: str, doc: Any) -> list[str]:
    name = name.strip().strip('"')
    out = [name]
    parts = name.split("@")
    if len(parts) > 2:
        out.append("@".join(parts[:2]))
    title = str(try_call(doc, "GetTitle") or "")
    stem = title.rsplit(".", 1)[0] if "." in title else title
    if len(parts) == 2 and stem:
        suffix = ".Assembly" if try_call(doc, "GetType") == C.DOC_ASSEMBLY else ".Part"
        out.append(f"{name}@{stem}{suffix}")
    return list(dict.fromkeys(out))


def find_dimension(doc: Any, name: str) -> tuple[Any, str]:
    for candidate in _candidates(name, doc):
        dim = try_call(doc, "Parameter", candidate)
        if dim is not None:
            return dim, candidate
    raise SwError(
        Code.NOT_FOUND,
        f'No dimension named "{name}" in the active document.',
        "Ask the user to click the feature or dimension, then call get_selection_context "
        "and copy a name from its 'dims' list exactly (for example D1@Boss-Extrude1).",
    )


def _read_si(dim: Any) -> float:
    raw = try_call(dim, "SystemValue")
    if raw is None:
        raw = call(dim, "GetSystemValue2", "")
    if isinstance(raw, (list, tuple)):
        raw = raw[0]
    return float(raw)


def _write_si(dim: Any, value: float) -> None:
    try:
        dim.SystemValue = value
    except Exception:  # noqa: BLE001 - older/newer API variant
        call(dim, "SetSystemValue2", value, 1)  # swSetValue_InThisConfiguration


def _rebuild(doc: Any) -> bool:
    result = try_call(doc, "EditRebuild3")
    if result is None:
        result = try_call(doc, "ForceRebuild3", True)
    return result is not False


def _problem_count(doc: Any) -> int | None:
    return try_call(try_call(doc, "Extension"), "GetWhatsWrongCount")


def set_dimension(doc: Any, name: str, new_value: float) -> dict:
    dim, resolved = find_dimension(doc, name)
    is_assembly = try_call(doc, "GetType") == C.DOC_ASSEMBLY
    kind = try_call(dim, "GetType")
    old_value, unit = dimension_value(dim)
    if kind == C.DIM_ANGULAR:
        target = math.radians(new_value)
    elif kind == C.DIM_INTEGER:
        target = float(round(new_value))
    else:
        target = new_value / 1000.0
    if try_call(dim, "ReadOnly") is True:
        raise SwError(
            Code.READ_ONLY,
            f"{resolved} is a driven (reference) dimension and cannot be changed directly.",
            "Change the dimension that drives it instead. Use get_selection_context to find it.",
        )

    old_si = _read_si(dim)
    problems_before = _problem_count(doc)
    _write_si(dim, target)
    rebuilt = _rebuild(doc)
    now_si = _read_si(dim)
    problems_after = _problem_count(doc)

    applied = abs(now_si - target) <= 1e-9 + 1e-6 * abs(target)
    new_problems = (
        problems_before is not None and problems_after is not None and problems_after > problems_before
    )
    if not rebuilt or not applied or new_problems:
        _write_si(dim, old_si)
        _rebuild(doc)
        why = "the value was not accepted" if not applied else "the model has rebuild errors with it"
        raise SwError(
            Code.REBUILD_FAILED,
            f"Setting {resolved} to {new_value} {unit} failed: {why}. The old value {old_value} {unit} was restored.",
            "Try a less extreme value, or tell the user this change breaks the model.",
        )

    new_read, _ = dimension_value(dim)
    result: dict[str, Any] = {
        "dimension": dimension_name(dim, is_assembly) or resolved,
        "old": old_value,
        "new": new_read,
        "unit": unit,
        "next": "Call save_document to keep the change.",
    }
    if old_value and new_read and unit == "mm":
        ratio = new_read / old_value
        if ratio >= 100 or ratio <= 0.01:
            result["warning"] = (
                f"The new value is {ratio:.3g} times the old one. Values are in millimeters. "
                "If that was a mistake, set it back."
            )
    return result
