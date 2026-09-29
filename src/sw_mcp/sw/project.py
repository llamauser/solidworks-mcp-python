"""Projects: a folder of parts for one machine, and the assembly that puts them together.

Every part of a machine is modeled in the SAME world coordinates as the finished machine.
The assembly then just inserts all parts at the origin: no mates, no positioning, and the
parts land exactly where they were designed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pythoncom
import win32com.client

from ..core.com_utils import as_list, call, call_with_out_ints, try_call
from ..core.errors import Code, SwError
from . import documents
from .modeling import Modeler, _m

DEFAULT_PROJECT = "Unsorted"


def projects_root() -> Path:
    env = os.environ.get("SW_MCP_PROJECTS")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / "Documents" / "SolidWorks Assistant"


def _clean(name: str) -> str:
    cleaned = re.sub(r'[<>:"|?*@\x00-\x1f]', "", name).strip().strip(".")
    return cleaned[:80]


def split_name(save_as: str) -> tuple[str, str]:
    """'V4 engine/crankshaft' -> ('V4 engine', 'crankshaft'); 'bracket' -> ('Unsorted', 'bracket')."""
    parts = [p for p in re.split(r"[\\/]+", save_as.strip()) if p.strip()]
    if not parts:
        raise SwError(Code.BAD_ARGUMENT, "save_as is empty.", 'Use "project/part", e.g. "V4 engine/crankshaft".')
    if len(parts) > 2:
        raise SwError(Code.BAD_ARGUMENT, "save_as can have one folder level only.",
                      'Use "project/part", e.g. "V4 engine/crankshaft".')
    project = _clean(parts[0]) if len(parts) == 2 else DEFAULT_PROJECT
    name = _clean(os.path.splitext(parts[-1])[0])
    if not project or not name:
        raise SwError(Code.BAD_ARGUMENT, f"'{save_as}' is not a usable name.", "Use letters, numbers and spaces.")
    return project, name


def part_path(save_as: str) -> Path:
    project, name = split_name(save_as)
    folder = projects_root() / project
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.SLDPRT"


def save_part(app: Any, doc: Any, save_as: str) -> dict:
    """Save into the project folder, replacing an older version of the part. SolidWorks cannot
    overwrite a file it has loaded, so saved windows holding it (the part, its assembly) are closed."""
    path = part_path(save_as)
    out: dict[str, Any] = {"saved_as": str(path)}
    if path.exists():
        released = documents.release_file(app, str(path), saving=doc)
        if released:
            out["closed_to_replace"] = released
    documents.save_document(doc, str(path), overwrite=True)
    return out


def list_project(project: str) -> dict:
    root = projects_root()
    if not project.strip():
        projects = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []
        return {"projects_folder": str(root), "projects": projects}
    folder = root / _clean(project)
    if not folder.exists():
        raise SwError(Code.NOT_FOUND, f"There is no project called '{project}'.",
                      "Call list_project with an empty name to see the projects.")
    files = [f for f in sorted(folder.glob("*.SLDPRT")) + sorted(folder.glob("*.SLDASM"))
             if not f.name.startswith("~$")]  # SolidWorks' lock files for open documents
    out = {
        "project": folder.name,
        "folder": str(folder),
        "parts": [f.stem for f in files if f.suffix.upper() == ".SLDPRT"],
        "assemblies": [f.stem for f in files if f.suffix.upper() == ".SLDASM"],
    }
    from . import design

    if design.exists(folder.name):
        out["design"] = design.summary(folder.name)
    return out


def _open(app: Any, path: str) -> tuple[Any, bool]:
    """The loaded document for `path`, and whether this call opened it."""
    doc = try_call(app, "GetOpenDocumentByName", path)
    if doc is not None:
        return doc, False
    documents.open_document(app, path)
    return try_call(app, "GetOpenDocumentByName", path) or try_call(app, "ActiveDoc"), True


def _activate(app: Any, title: str) -> None:
    try:
        call_with_out_ints(app, "ActivateDoc3", title, False, 0, n_out=1)
    except Exception:  # noqa: BLE001 - older API
        try_call(app, "ActivateDoc", title)


def _offset(a: tuple[list[float], list[float]], b: tuple[list[float], list[float]]) -> list[float]:
    return [(a[0][i] + a[1][i]) / 2 - (b[0][i] + b[1][i]) / 2 for i in range(3)]


def _move_component(app: Any, comp: Any, offset_mm: list[float]) -> bool:
    """Shift a component by -offset (fixes AddComponent placing it off the origin)."""
    try:
        xform = call(comp, "Transform2")
        data = [float(v) for v in as_list(call(xform, "ArrayData"))]
        for i in range(3):
            data[9 + i] -= offset_mm[i] / 1000
        mu = call(app, "GetMathUtility")
        new = call(mu, "CreateTransform", win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, data))
        comp.Transform2 = new
        return True
    except Exception:  # noqa: BLE001
        return False


def make_assembly(app: Any, project: str, parts: str, name: str) -> dict:
    folder = projects_root() / _clean(project)
    if not folder.exists():
        raise SwError(Code.NOT_FOUND, f"There is no project called '{project}'.",
                      "Save parts first with build_part(save_as=\"project/part\").")
    available = {f.stem.lower(): f for f in folder.glob("*.SLDPRT") if not f.name.startswith("~$")}
    wanted = [p.strip() for p in parts.split(",") if p.strip()] if parts.strip().lower() not in ("", "all") else []
    if wanted:
        missing = [w for w in wanted if w.lower() not in available]
        if missing:
            raise SwError(Code.NOT_FOUND, f"Not in project '{project}': {', '.join(missing)}.",
                          f"Available parts: {', '.join(sorted(f.stem for f in available.values()))}.")
        files = [available[w.lower()] for w in wanted]
    else:
        files = sorted(available.values())
    if not files:
        raise SwError(Code.NOT_FOUND, f"Project '{project}' has no parts yet.", "Build and save the parts first.")

    template = try_call(app, "GetUserPreferenceStringValue", 9)  # swDefaultTemplateAssembly
    if not template:
        template = try_call(app, "GetDocumentTemplate", 2, "", 0, 0.0, 0.0)
    if not template:
        raise SwError(Code.SW_ERROR, "No default assembly template is set in SolidWorks.",
                      "Ask the user to set one in Tools > Options > Default Templates.")

    part_boxes = {}
    opened_here = []
    for f in files:  # parts must be loaded before they can be inserted
        doc, opened = _open(app, str(f))
        part_boxes[f.stem] = Modeler(app, doc).bbox_mm() if doc is not None else None
        if opened and doc is not None:
            opened_here.append(try_call(doc, "GetTitle"))
    asm = call(app, "NewDocument", template, 0, 0.0, 0.0)
    if asm is None:
        raise SwError(Code.SW_ERROR, "SolidWorks could not create an assembly.", "Check the assembly template.")
    title = try_call(asm, "GetTitle")

    placed, warnings = [], []
    for f in files:
        _activate(app, title)
        comp = try_call(asm, "AddComponent5", str(f), 0, "", False, "", 0.0, 0.0, 0.0)
        if comp is None:
            comp = try_call(asm, "AddComponent4", str(f), "", 0.0, 0.0, 0.0)
        if comp is None:
            warnings.append(f"{f.stem}: SolidWorks could not insert it")
            continue
        placed.append(f.stem)
        want = part_boxes.get(f.stem)
        got = as_list(try_call(comp, "GetBox", False, False))
        if want and len(got) >= 6:
            off = _offset((_m(got[0:3]), _m(got[3:6])), want)
            if max(abs(v) for v in off) > 0.05:
                _move_component(app, comp, off)
                again = as_list(try_call(comp, "GetBox", False, False))  # check the correction worked
                if len(again) >= 6:
                    off = _offset((_m(again[0:3]), _m(again[3:6])), want)
                if max(abs(v) for v in off) > 0.05:
                    warnings.append(f"{f.stem}: placed {max(abs(v) for v in off):.1f} mm off its design position")

    path = folder / f"{_clean(name) or 'Assembly'}.SLDASM"
    if path.exists():
        documents.release_file(app, str(path), saving=asm)
    documents.save_document(asm, str(path), overwrite=True)
    for part_title in opened_here:  # the assembly keeps them loaded; their own windows are clutter
        if part_title:
            try_call(app, "CloseDoc", part_title)
    _activate(app, try_call(asm, "GetTitle") or title)
    try_call(asm, "ViewZoomtofit2")
    out: dict[str, Any] = {"assembly": str(path), "components": placed}
    box = as_list(try_call(asm, "GetBox", 0))
    if len(box) >= 6:
        lo, hi = _m(box[0:3]), _m(box[3:6])
        out["size_mm"] = [round(hi[i] - lo[i], 2) for i in range(3)]
    if warnings:
        out["warnings"] = warnings
    try:
        from . import design

        design.record_assembly(folder.name, path.name, placed, out.get("size_mm"), warnings)
    except Exception:  # noqa: BLE001 - never break the assembly for the record
        pass
    out["next"] = "Tell the user the assembly is ready and where it was saved."
    return out
