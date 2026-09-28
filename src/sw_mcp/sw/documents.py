"""Open / save / describe documents, with newer-API-first fallbacks for older releases."""

from __future__ import annotations

import os
from typing import Any

from ..core.com_utils import call, call_with_out_ints, null_dispatch, try_call
from ..core.errors import Code, SwError
from . import constants as C


def clean_path(raw: str) -> str:
    path = os.path.expandvars(os.path.expanduser(raw.strip().strip('"').strip("'")))
    return os.path.normpath(os.path.abspath(path))


def doc_summary(doc: Any) -> dict:
    out: dict[str, Any] = {
        "name": try_call(doc, "GetTitle"),
        "type": C.DOC_TYPE_NAMES.get(try_call(doc, "GetType"), "unknown"),
        "path": try_call(doc, "GetPathName") or "",
    }
    dirty = try_call(doc, "GetSaveFlag")
    if dirty is not None:
        out["unsaved_changes"] = bool(dirty)
    return out


def active_sketch_name(doc: Any) -> str | None:
    sketch = try_call(try_call(doc, "SketchManager"), "ActiveSketch")
    if sketch is None:
        return None
    return try_call(sketch, "Name") or "(unnamed sketch)"


# ---------------------------------------------------------------- open
def _open_doc7(app: Any, path: str, doc_type: int) -> tuple[Any, int, int]:
    spec = call(app, "GetOpenDocSpec", path)
    spec.DocumentType = doc_type
    spec.Silent = True
    doc = call(app, "OpenDoc7", spec)
    return doc, int(try_call(spec, "Error", default=0) or 0), int(try_call(spec, "Warning", default=0) or 0)


def _open_doc6(app: Any, path: str, doc_type: int) -> tuple[Any, int, int]:
    doc, (err, warn) = call_with_out_ints(app, "OpenDoc6", path, doc_type, C.OPEN_SILENT, "")
    return doc, err, warn


def open_document(app: Any, raw_path: str) -> dict:
    path = clean_path(raw_path)
    if not os.path.isfile(path):
        raise SwError(
            Code.FILE_NOT_FOUND,
            f"File not found: {path}",
            "Check the spelling and use the full path, for example C:\\Parts\\bracket.SLDPRT.",
        )
    ext = os.path.splitext(path)[1].lower()
    doc_type = C.EXT_TO_DOC_TYPE.get(ext)
    if doc_type is None:
        raise SwError(
            Code.BAD_ARGUMENT,
            f"'{ext}' is not a SolidWorks document. Only .SLDPRT, .SLDASM and .SLDDRW can be opened.",
            "Pass the path of a part, assembly or drawing file.",
        )

    already = try_call(app, "GetOpenDocumentByName", path)
    if already is not None:
        title = try_call(already, "GetTitle")
        if title:
            try:
                call_with_out_ints(app, "ActivateDoc3", title, False, 0, n_out=1)
            except Exception:  # noqa: BLE001 - older API
                try:
                    call_with_out_ints(app, "ActivateDoc2", title, True, n_out=1)
                except Exception:  # noqa: BLE001 - activation is cosmetic here
                    pass
        doc = try_call(app, "ActiveDoc") or already
        return {"opened": doc_summary(doc), "note": "The file was already open; it is now the active window."}

    last_exc: BaseException | None = None
    doc, err, warn = None, 0, 0
    for opener in (_open_doc7, _open_doc6):
        try:
            doc, err, warn = opener(app, path, doc_type)
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 - try the older API next
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    if doc is None:
        reasons = C.decode_flags(err, C.FILE_LOAD_ERRORS) or ["SolidWorks gave no reason"]
        raise SwError(
            Code.OPEN_FAILED,
            f"SolidWorks could not open {os.path.basename(path)}: {'; '.join(reasons)}.",
            "Tell the user the reason. Do not retry the same file unless the user fixes the problem.",
        )
    result: dict[str, Any] = {"opened": doc_summary(doc)}
    warnings = C.decode_flags(warn, C.FILE_LOAD_WARNINGS)
    if warnings:
        result["warnings"] = warnings
    return result


# ---------------------------------------------------------------- save
def _save_in_place(doc: Any) -> tuple[bool, int, int]:
    try:
        ok, (err, warn) = call_with_out_ints(doc, "Save3", C.SAVE_SILENT)
        return bool(ok), err, warn
    except Exception:  # noqa: BLE001 - older API
        err = call(doc, "Save2", True)
        return int(err or 0) == 0, int(err or 0), 0


def _save_as(doc: Any, path: str) -> tuple[bool, int, int]:
    ext = call(doc, "Extension")
    attempts = (
        lambda: call_with_out_ints(ext, "SaveAs", path, C.SAVE_AS_CURRENT_VERSION, C.SAVE_SILENT, null_dispatch()),
        lambda: call_with_out_ints(
            ext, "SaveAs3", path, C.SAVE_AS_CURRENT_VERSION, C.SAVE_SILENT, null_dispatch(), null_dispatch()
        ),
        lambda: call_with_out_ints(doc, "SaveAs4", path, C.SAVE_AS_CURRENT_VERSION, C.SAVE_SILENT),
    )
    last: BaseException | None = None
    for attempt in attempts:
        try:
            ok, (err, warn) = attempt()
            return bool(ok), err, warn
        except Exception as exc:  # noqa: BLE001 - try the next API generation
            last = exc
    assert last is not None
    raise last


def save_document(doc: Any, save_as_path: str, overwrite: bool) -> dict:
    if not save_as_path.strip():
        path = try_call(doc, "GetPathName") or ""
        if not path:
            raise SwError(
                Code.BAD_ARGUMENT,
                "This document has never been saved, so it has no file name yet.",
                'Call save_document with save_as_path, for example "C:\\Parts\\new_part.SLDPRT".',
            )
        ok, err, warn = _save_in_place(doc)
        kind = "saved"
    else:
        path = clean_path(save_as_path)
        ext = os.path.splitext(path)[1].lower()
        doc_type = try_call(doc, "GetType")
        native_type = C.NATIVE_EXTS.get(ext)
        if native_type is None and ext not in C.EXPORT_EXTS:
            allowed = ", ".join(sorted({*C.NATIVE_EXTS, *C.EXPORT_EXTS}))
            raise SwError(Code.BAD_ARGUMENT, f"Unknown file extension '{ext}'.", f"Use one of: {allowed}.")
        if native_type is not None and native_type != doc_type:
            want = [e for e, t in C.NATIVE_EXTS.items() if t == doc_type]
            raise SwError(
                Code.BAD_ARGUMENT,
                f"A {C.DOC_TYPE_NAMES.get(doc_type, 'document')} cannot be saved as '{ext}'.",
                f"Use the {want[0] if want else 'matching'} extension, or an export format like .step or .pdf.",
            )
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            raise SwError(Code.FILE_NOT_FOUND, f"Folder does not exist: {folder}", "Use an existing folder.")
        if os.path.exists(path) and not overwrite:
            raise SwError(
                Code.FILE_EXISTS,
                f"{path} already exists.",
                "Ask the user whether to replace it. If yes, call again with overwrite=true.",
            )
        ok, err, warn = _save_as(doc, path)
        kind = "saved_as" if native_type is not None else "exported"

    if not ok or err:
        reasons = C.decode_flags(err, C.FILE_SAVE_ERRORS) or ["SolidWorks gave no reason"]
        raise SwError(
            Code.SAVE_FAILED,
            f"Saving to {path} failed: {'; '.join(reasons)}.",
            "Tell the user the reason. Check the file is not open in another program.",
        )
    result: dict[str, Any] = {kind: path}
    if os.path.isfile(path):
        result["size_kb"] = round(os.path.getsize(path) / 1024, 1)
    warnings = C.decode_flags(warn, C.FILE_SAVE_WARNINGS)
    if warnings:
        result["warnings"] = warnings
    if kind == "exported":
        result["note"] = "Exported a copy. The open SolidWorks document itself was not renamed."
    return result


# ---------------------------------------------------------------- managing open windows
def open_documents(app: Any) -> list[Any]:
    docs = [d for d in (try_call(app, "GetDocuments") or ()) if d is not None]
    if docs:
        return docs
    out, doc, n = [], try_call(app, "GetFirstDocument"), 0  # older API
    while doc is not None and n < 500:
        out.append(doc)
        doc, n = try_call(doc, "GetNext"), n + 1
    return out


def _visible(doc: Any) -> bool:
    # Parts loaded only because an open assembly uses them have no window of their own.
    return try_call(doc, "Visible") is not False


def list_documents(app: Any) -> dict:
    active = try_call(try_call(app, "ActiveDoc"), "GetTitle")
    docs = []
    for doc in open_documents(app):
        if not _visible(doc):
            continue
        info = doc_summary(doc)
        info["active"] = info["name"] == active
        docs.append(info)
    unsaved = [d["name"] for d in docs if d.get("unsaved_changes") or not d["path"]]
    out: dict[str, Any] = {"open": len(docs), "documents": docs[:40]}
    if unsaved:
        out["unsaved"] = unsaved
    return out


def find_document(app: Any, name: str) -> Any:
    wanted = name.strip().strip('"').lower()
    stem = os.path.splitext(os.path.basename(wanted))[0]
    for doc in open_documents(app):
        title = str(try_call(doc, "GetTitle") or "").lower()
        path = str(try_call(doc, "GetPathName") or "").lower()
        if wanted in (title, path) or stem == os.path.splitext(title)[0] or (path and stem == os.path.splitext(os.path.basename(path))[0]):
            return doc
    raise SwError(Code.NOT_FOUND, f"No open document called '{name}'.",
                  "Use manage_documents(action=\"list\") to see the open documents.")


def activate_document(app: Any, name: str) -> dict:
    doc = find_document(app, name)
    title = try_call(doc, "GetTitle")
    try:
        call_with_out_ints(app, "ActivateDoc3", title, False, 0, n_out=1)
    except Exception:  # noqa: BLE001 - older API
        call(app, "ActivateDoc", title)
    return {"active": doc_summary(try_call(app, "ActiveDoc") or doc)}


def _is_unsaved(doc: Any) -> bool:
    return bool(try_call(doc, "GetSaveFlag")) or not try_call(doc, "GetPathName")


def close_documents(app: Any, name: str, discard_unsaved: bool) -> dict:
    """name: "" = the active window, "all" = every window, "others" = all but the active one,
    "new" = never-saved windows (Part1, Part2, ...), otherwise a document name."""
    key = name.strip().lower()
    active_title = try_call(try_call(app, "ActiveDoc"), "GetTitle")
    if key == "all":
        targets = [d for d in open_documents(app) if _visible(d)]
    elif key == "others":
        targets = [d for d in open_documents(app) if _visible(d) and try_call(d, "GetTitle") != active_title]
    elif key == "new":
        targets = [d for d in open_documents(app) if _visible(d) and not try_call(d, "GetPathName")]
    elif key in ("", "active"):
        active = try_call(app, "ActiveDoc")
        if active is None:
            raise SwError(Code.NO_ACTIVE_DOC, "No document is open.", "Nothing to close.")
        targets = [active]
    else:
        targets = [find_document(app, name)]
    closed, kept = [], []
    for doc in targets:
        title = try_call(doc, "GetTitle")
        if _is_unsaved(doc) and not discard_unsaved:
            kept.append(title)
            continue
        call(app, "CloseDoc", title)
        closed.append(title)
    out: dict[str, Any] = {"closed": closed}
    if kept:
        out["kept_unsaved"] = kept
        out["note"] = ("These have unsaved changes and were NOT closed. Ask the user: save them with "
                       "save_document, or close them with discard_unsaved=true (their changes are lost).")
    out["still_open"] = sum(1 for d in open_documents(app) if _visible(d))
    return out


def release_file(app: Any, path: str, saving: Any = None) -> list[str]:
    """Close the windows that hold `path`, so the file can be overwritten: the document itself and
    the open assemblies in its folder (they load it). Only documents without unsaved changes are
    closed. Returns their titles."""
    target = os.path.normcase(os.path.abspath(path))
    folder = os.path.dirname(target)
    own = try_call(saving, "GetTitle") if saving is not None else None
    holders = []
    for doc in open_documents(app):
        dpath = os.path.normcase(str(try_call(doc, "GetPathName") or ""))
        title = try_call(doc, "GetTitle")
        if not dpath or title == own:
            continue
        if dpath == target or (dpath.endswith(".sldasm") and os.path.dirname(dpath) == folder):
            holders.append((dpath != target, doc, title))
    closed = []
    for _, doc, title in sorted(holders, key=lambda h: not h[0]):  # assemblies first, then the part
        if _is_unsaved(doc):
            raise SwError(Code.SAVE_FAILED,
                          f"{title} is open with unsaved changes and uses {os.path.basename(path)}, so it cannot be replaced.",
                          "Ask the user to save or close that window, then build again.")
        call(app, "CloseDoc", title)
        closed.append(title)
    return closed
