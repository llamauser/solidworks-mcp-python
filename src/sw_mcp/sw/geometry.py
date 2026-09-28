"""Turn SolidWorks entities into small dicts (mm, mm2, degrees; rounded to 1 micrometer).

Every reader is best effort: a missing API member on some SolidWorks version drops that
field instead of failing the whole call.
"""

from __future__ import annotations

import math
from typing import Any

from ..core.com_utils import as_list, call, deg, mm, mm2, point_mm, try_call, vec
from . import constants as C

MAX_DIMS_PER_FEATURE = 6
MAX_LOOP = 200


# ---------------------------------------------------------------- dimensions
def dimension_name(dim: Any, is_assembly: bool, feature_name: str | None = None) -> str | None:
    """Name usable with set_dimension.

    Parts: 'D1@Boss-Extrude1'. Assemblies keep the full name, because the component
    part of the name is needed to find the dimension.
    """
    full = try_call(dim, "FullName")
    if full:
        full = str(full)
        if is_assembly:
            return full
        parts = full.split("@")
        return "@".join(parts[:2]) if len(parts) >= 2 else full
    short = try_call(dim, "Name")
    if short and feature_name:
        return f"{short}@{feature_name}"
    return str(short) if short else None


def dimension_value(dim: Any) -> tuple[float | None, str]:
    """(value, unit) in the units tools use: mm for lengths, deg for angles."""
    kind = try_call(dim, "GetType")
    raw = try_call(dim, "SystemValue")
    if raw is None:
        raw = try_call(dim, "GetSystemValue2", "")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return None, ""
    if kind == C.DIM_ANGULAR:
        return deg(raw), "deg"
    if kind == C.DIM_INTEGER:
        return float(raw), "count"
    return mm(raw), "mm"


def describe_dimension(dim: Any, is_assembly: bool, feature_name: str | None = None) -> dict:
    value, unit = dimension_value(dim)
    out: dict[str, Any] = {"name": dimension_name(dim, is_assembly, feature_name), "value": value, "unit": unit}
    if try_call(dim, "ReadOnly") is True:
        out["read_only"] = True
    return out


def dimension_from_display(display_dim: Any) -> Any:
    dim = try_call(display_dim, "GetDimension2", 0)
    if dim is None:
        dim = try_call(display_dim, "GetDimension")
    return dim


def _display_dims(feature: Any):
    disp = try_call(feature, "GetFirstDisplayDimension")
    n = 0
    while disp is not None and n < MAX_LOOP:
        yield disp
        disp = try_call(feature, "GetNextDisplayDimension", disp)
        n += 1


def _sub_features(feature: Any):
    sub = try_call(feature, "GetFirstSubFeature")
    n = 0
    while sub is not None and n < MAX_LOOP:
        yield sub
        sub = try_call(sub, "GetNextSubFeature")
        n += 1


def feature_dimensions(feature: Any, is_assembly: bool, limit: int = MAX_DIMS_PER_FEATURE) -> list[dict]:
    """Dimensions of a feature and of its sketches (e.g. extrude depth + sketch widths)."""
    out: list[dict] = []
    seen: set = set()
    for owner in [feature, *_sub_features(feature)]:
        owner_name = try_call(owner, "Name")
        for disp in _display_dims(owner):
            dim = dimension_from_display(disp)
            if dim is None:
                continue
            info = describe_dimension(dim, is_assembly, owner_name)
            if info["name"] in seen:
                continue
            seen.add(info["name"])
            out.append(info)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------- features
def describe_feature(feature: Any, is_assembly: bool, with_dims: bool = True) -> dict:
    out: dict[str, Any] = {"name": try_call(feature, "Name"), "feature_type": try_call(feature, "GetTypeName2")}
    if with_dims:
        dims = feature_dimensions(feature, is_assembly)
        if dims:
            out["dims"] = dims
    return out


# ---------------------------------------------------------------- faces
def surface_kind(surface: Any) -> str:
    for name, kind in (("IsPlane", "plane"), ("IsCylinder", "cylinder"), ("IsCone", "cone"),
                       ("IsSphere", "sphere"), ("IsTorus", "torus")):
        if try_call(surface, name) is True:
            return kind
    return "freeform" if surface is not None else "unknown"


def describe_face(face: Any, is_assembly: bool) -> dict:
    out: dict[str, Any] = {"type": "face"}
    surface = try_call(face, "GetSurface")
    kind = surface_kind(surface)
    out["surface"] = kind
    if kind == "plane":
        normal = vec(try_call(face, "Normal"))
        if normal:
            out["normal"] = normal
        params = as_list(try_call(surface, "PlaneParams"))
        if len(params) >= 6:
            out["point_mm"] = point_mm(params[3:6])
    elif kind == "cylinder":
        params = as_list(try_call(surface, "CylinderParams"))
        if len(params) >= 7:
            out["axis_point_mm"] = point_mm(params[0:3])
            out["axis"] = vec(params[3:6])
            out["radius_mm"] = mm(params[6])
            out["diameter_mm"] = mm(2 * float(params[6]))
    elif kind == "sphere":
        params = as_list(try_call(surface, "SphereParams"))
        if len(params) >= 4:
            out["center_mm"] = point_mm(params[0:3])
            out["radius_mm"] = mm(params[3])
    area = try_call(face, "GetArea")
    if area is not None:
        out["area_mm2"] = mm2(area)
    feature = try_call(face, "GetFeature")
    if feature is not None:
        out["feature"] = try_call(feature, "Name")
        dims = feature_dimensions(feature, is_assembly)
        if dims:
            out["dims"] = dims
    return out


# ---------------------------------------------------------------- edges / vertices
def describe_edge(edge: Any) -> dict:
    out: dict[str, Any] = {"type": "edge"}
    curve = try_call(edge, "GetCurve")
    params = as_list(try_call(edge, "GetCurveParams2"))
    start = params[0:3] if len(params) >= 6 else None
    end = params[3:6] if len(params) >= 6 else None
    closed = bool(start and end and math.dist(start, end) < 1e-9)
    if try_call(curve, "IsLine") is True:
        out["kind"] = "line"
    elif try_call(curve, "IsCircle") is True:
        out["kind"] = "circle" if closed else "arc"
        cp = as_list(try_call(curve, "CircleParams"))
        if len(cp) >= 7:
            out["center_mm"] = point_mm(cp[0:3])
            out["axis"] = vec(cp[3:6])
            out["radius_mm"] = mm(cp[6])
            out["diameter_mm"] = mm(2 * float(cp[6]))
    else:
        out["kind"] = "curve"
    length = None
    if curve is not None and len(params) >= 8:
        length = try_call(curve, "GetLength3", params[6], params[7])
        if length is None:
            length = try_call(curve, "GetLength2", params[6], params[7])
    if length is None and start and end and out["kind"] == "line":
        length = math.dist(start, end)
    if length is not None:
        out["length_mm"] = mm(length)
    if start and not closed:
        out["start_mm"] = point_mm(start)
        out["end_mm"] = point_mm(end)
    features = []
    for face in as_list(try_call(edge, "GetTwoAdjacentFaces2")):
        name = try_call(try_call(face, "GetFeature"), "Name")
        if name and name not in features:
            features.append(name)
    if features:
        out["features"] = features
    return out


def describe_vertex(vertex: Any) -> dict:
    return {"type": "vertex", "point_mm": point_mm(try_call(vertex, "GetPoint"))}


# ---------------------------------------------------------------- sketch entities
def describe_sketch_segment(seg: Any) -> dict:
    kind = C.SKETCH_SEG_NAMES.get(try_call(seg, "GetType"), "segment")
    out: dict[str, Any] = {"type": "sketch_segment", "kind": kind}
    length = try_call(seg, "GetLength")
    if length is not None:
        out["length_mm"] = mm(length)
    if kind == "arc":
        radius = try_call(seg, "GetRadius")
        if radius is not None:
            out["radius_mm"] = mm(radius)
    if try_call(seg, "ConstructionGeometry") is True:
        out["construction"] = True
    return out


def describe_sketch_point(pt: Any) -> dict:
    xyz = [try_call(pt, "X"), try_call(pt, "Y"), try_call(pt, "Z")]
    return {"type": "sketch_point", "point_mm": point_mm(xyz) if None not in xyz else None}


# ---------------------------------------------------------------- assemblies
def describe_component(comp: Any) -> dict:
    out: dict[str, Any] = {"type": "component", "name": try_call(comp, "Name2")}
    path = try_call(comp, "GetPathName")
    if path:
        out["file"] = path
    fixed = try_call(comp, "IsFixed")
    if fixed is not None:
        out["fixed"] = bool(fixed)
    return out


def describe_mate(obj: Any, is_assembly: bool = True) -> dict:
    """obj is the mate feature (usual case) or the IMate2 itself."""
    feature = obj
    mate = try_call(obj, "GetSpecificFeature2")
    if mate is None:
        mate, feature = obj, None
    out: dict[str, Any] = {"type": "mate"}
    if feature is not None:
        out["name"] = try_call(feature, "Name")
    kind = try_call(mate, "Type")
    out["mate_type"] = C.MATE_TYPE_NAMES.get(kind, f"type_{kind}")
    components = []
    count = try_call(mate, "GetMateEntityCount", default=0) or 0
    for i in range(min(int(count), 8)):
        entity = try_call(mate, "MateEntity", i)
        name = try_call(try_call(entity, "ReferenceComponent"), "Name2")
        if name and name not in components:
            components.append(name)
    if components:
        out["components"] = components
    if feature is not None:
        dims = feature_dimensions(feature, is_assembly, limit=2)
        if dims:
            out["dims"] = dims
    return out


# ---------------------------------------------------------------- dispatcher
def describe_selected(sel_type: int, obj: Any, is_assembly: bool) -> dict:
    if sel_type == C.SEL_FACES:
        return describe_face(obj, is_assembly)
    if sel_type == C.SEL_EDGES:
        return describe_edge(obj)
    if sel_type == C.SEL_VERTICES:
        return describe_vertex(obj)
    if sel_type == C.SEL_DIMENSIONS:
        dim = dimension_from_display(obj) or obj
        return {"type": "dimension", **describe_dimension(dim, is_assembly)}
    if sel_type == C.SEL_MATES:
        return describe_mate(obj, is_assembly)
    if sel_type == C.SEL_COMPONENTS:
        return describe_component(obj)
    if sel_type in (C.SEL_DATUMPLANES, C.SEL_DATUMAXES, C.SEL_DATUMPOINTS):
        label = {C.SEL_DATUMPLANES: "plane", C.SEL_DATUMAXES: "axis", C.SEL_DATUMPOINTS: "point"}[sel_type]
        return {"type": label, "name": try_call(obj, "Name")}
    if sel_type in (C.SEL_BODYFEATURES, C.SEL_SKETCHES):
        kind = "sketch" if sel_type == C.SEL_SKETCHES else "feature"
        return {"type": kind, **describe_feature(obj, is_assembly)}
    if sel_type == C.SEL_SKETCHSEGS:
        return describe_sketch_segment(obj)
    if sel_type == C.SEL_SKETCHPOINTS:
        return describe_sketch_point(obj)
    name = try_call(obj, "Name")
    out: dict[str, Any] = {"type": "other", "sw_select_type": sel_type}
    if name:
        out["name"] = name
    return out


def selection_context(doc: Any, max_items: int) -> dict:
    is_assembly = try_call(doc, "GetType") == C.DOC_ASSEMBLY
    sel = call(doc, "SelectionManager")
    count = int(try_call(sel, "GetSelectedObjectCount2", -1, default=0) or 0)
    items = []
    for i in range(1, min(count, max_items) + 1):
        sel_type = try_call(sel, "GetSelectedObjectType3", i, -1, default=0)
        obj = try_call(sel, "GetSelectedObject6", i, -1)
        try:
            item = describe_selected(int(sel_type or 0), obj, is_assembly)
        except Exception as exc:  # noqa: BLE001 - one odd item must not hide the others
            item = {"type": "unreadable", "sw_select_type": sel_type, "reason": str(exc)[:120]}
        if is_assembly:
            comp = try_call(sel, "GetSelectedObjectsComponent4", i, -1)
            comp_name = try_call(comp, "Name2")
            if comp_name and item.get("type") != "component":
                item["component"] = comp_name
        items.append(item)
    out: dict[str, Any] = {
        "document": try_call(doc, "GetTitle"),
        "selected_count": count,
        "items": items,
    }
    if count > max_items:
        out["truncated"] = f"showing {max_items} of {count}; call again with a larger max_items to see more"
    if count == 0:
        out["hint"] = ("Nothing is selected. Ask the user to click a face, edge, dimension or mate in "
                       "SolidWorks, then call get_selection_context again.")
    return out
