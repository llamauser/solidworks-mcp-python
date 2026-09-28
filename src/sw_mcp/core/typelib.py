"""Read SolidWorks enum values (e.g. swFmAEMRotaryMotor) from the type libraries installed with
SolidWorks itself, so rarely used APIs work without hard-coding numbers that differ between
releases or that are not documented anywhere we can check.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import pythoncom

log = logging.getLogger(__name__)

TLB_NAMES = ("swconst.tlb", "swmotionstudy.tlb", "sldworks.tlb")


def _tlb_files() -> list[Path]:
    from .connection import find_solidworks_exe

    exe = find_solidworks_exe()
    if not exe:
        return []
    folder = Path(exe).parent
    return [folder / n for n in TLB_NAMES if (folder / n).exists()]


@lru_cache(maxsize=1)
def constants() -> dict[str, int]:
    """Every enum member name -> value found in SolidWorks' type libraries (empty if none)."""
    out: dict[str, int] = {}
    for path in _tlb_files():
        try:
            lib = pythoncom.LoadTypeLib(str(path))
        except pythoncom.com_error:
            log.info("could not load %s", path)
            continue
        for i in range(lib.GetTypeInfoCount()):
            try:
                info = lib.GetTypeInfo(i)
                attr = info.GetTypeAttr()
                if attr.typekind != pythoncom.TKIND_ENUM:
                    continue
                for j in range(attr.cVars):
                    var = info.GetVarDesc(j)
                    names = info.GetNames(var.memid)
                    if names:
                        out[names[0]] = int(var.value)
            except (pythoncom.com_error, TypeError, ValueError):
                continue
    log.info("loaded %d SolidWorks constants from %s", len(out), [p.name for p in _tlb_files()])
    return out


def value(*names: str) -> int | None:
    """The value of the first name that exists."""
    table = constants()
    for name in names:
        if name in table:
            return table[name]
    return None


def search(*parts: str) -> dict[str, int]:
    """Enum members whose name contains ALL the given parts (case-insensitive)."""
    wanted = [p.lower() for p in parts]
    return {k: v for k, v in constants().items() if all(w in k.lower() for w in wanted)}


def member_names(obj) -> list[str]:
    """Method/property names a COM object exposes (for diagnosing unknown interfaces)."""
    names: set[str] = set()
    try:
        info = obj._oleobj_.GetTypeInfo()
        attr = info.GetTypeAttr()
        for i in range(attr.cFuncs):
            fd = info.GetFuncDesc(i)
            names.add(info.GetNames(fd.memid)[0])
    except Exception:  # noqa: BLE001 - diagnostics only
        pass
    return sorted(n for n in names if not n.startswith("_") and n not in (
        "QueryInterface", "AddRef", "Release", "GetTypeInfoCount", "GetTypeInfo", "GetIDsOfNames", "Invoke"))


def clear_cache() -> None:
    constants.cache_clear()


if os.environ.get("SW_MCP_DUMP_CONSTANTS"):  # debugging aid: python -c "import sw_mcp.core.typelib"
    print(len(constants()))
