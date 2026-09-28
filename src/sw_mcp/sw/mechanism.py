"""Make an assembly move: joints between parts, turning a part, and SolidWorks Motion Studies.

Parts built with build_part(save_as=...) are modeled in the machine's own coordinates, so in the
assembly every shaft already sits exactly in its bore. connect_parts finds those coaxial
cylinder pairs and adds concentric mates; parts with no joint are fixed in place. Then a part
can be turned (move_mechanism) and the others follow through their mates.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import pythoncom
import win32com.client

from ..core import typelib
from ..core.com_utils import as_list, call, null_dispatch, try_call
from ..core.errors import Code, SwError
from .modeling import _m

log = logging.getLogger(__name__)

MATE_CONCENTRIC = 1       # swMateType_e
ALIGN_CLOSEST = 2         # swMateAlign_e
AXIS_TOL_MM = 0.05        # shaft and bore axes must coincide within this
CLEARANCE_MM = 1.0        # largest radius difference still treated as "shaft in bore"


# ---------------------------------------------------------------- geometry helpers (pure)
def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return [c / n for c in v]


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _sub(a, b) -> list[float]:
    return [x - y for x, y in zip(a, b)]


def _cross(a, b) -> list[float]:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def apply_transform(data: list[float], p: list[float]) -> list[float]:
    """SolidWorks MathTransform ArrayData: [0..8] rotation (row-major), [9..11] translation (m),
    [12] scale. Points are row vectors: p' = p . R * scale + t (p and t in the same units)."""
    r, t, s = data[0:9], data[9:12], data[12] if len(data) > 12 and data[12] else 1.0
    return [(p[0] * r[0 + k] + p[1] * r[3 + k] + p[2] * r[6 + k]) * s + t[k] for k in range(3)]


def rotation_about(axis: list[float], deg: float, point: list[float]) -> list[float]:
    """ArrayData (translation in meters) for a right-hand rotation about a line (point in mm)."""
    u = _unit(axis)
    th = math.radians(deg)
    c, s, v = math.cos(th), math.sin(th), 1 - math.cos(th)
    x, y, z = u
    m = [[c + x * x * v, x * y * v - z * s, x * z * v + y * s],
         [y * x * v + z * s, c + y * y * v, y * z * v - x * s],
         [z * x * v - y * s, z * y * v + x * s, c + z * z * v]]
    r = [m[j][i] for i in range(3) for j in range(3)]  # row-vector form = transpose of M
    pm = [c_ / 1000 for c_ in point]
    rotated = [pm[0] * r[0 + k] + pm[1] * r[3 + k] + pm[2] * r[6 + k] for k in range(3)]
    t = [pm[k] - rotated[k] for k in range(3)]
    return r + t + [1.0, 0.0, 0.0, 0.0]


def compose(first: list[float], then: list[float]) -> list[float]:
    """Row-vector transforms: apply `first`, then `then`."""
    r1, t1, r2, t2 = first[0:9], first[9:12], then[0:9], then[9:12]
    r = [sum(r1[i * 3 + k] * r2[k * 3 + j] for k in range(3)) for i in range(3) for j in range(3)]
    t = [sum(t1[k] * r2[k * 3 + j] for k in range(3)) + t2[j] for j in range(3)]
    return r + t + [1.0, 0.0, 0.0, 0.0]


IDENTITY = [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, 0, 0, 0]


@dataclass
class CylFace:
    component: str
    face: Any
    origin: list[float]  # mm, assembly space
    axis: list[float]
    radius: float        # mm
    lo: float            # extent along the axis (mm, measured from origin)
    hi: float


def coaxial(a: CylFace, b: CylFace) -> bool:
    if abs(_dot(a.axis, b.axis)) < 0.9999:
        return False
    off = _cross(_sub(b.origin, a.origin), a.axis)
    if math.sqrt(_dot(off, off)) > AXIS_TOL_MM:
        return False
    if abs(a.radius - b.radius) > CLEARANCE_MM:
        return False
    shift = _dot(_sub(b.origin, a.origin), a.axis)
    sign = 1.0 if _dot(a.axis, b.axis) > 0 else -1.0
    b_lo, b_hi = sorted((shift + sign * b.lo, shift + sign * b.hi))
    return min(a.hi, b_hi) - max(a.lo, b_lo) > 0.1  # they overlap along the axis


def find_joints(faces: list[CylFace]) -> list[tuple[CylFace, CylFace]]:
    """One joint per (component pair, axis line): the closest-fitting coaxial faces."""
    best: dict[tuple, tuple[float, CylFace, CylFace]] = {}
    for i, a in enumerate(faces):
        for b in faces[i + 1:]:
            if a.component == b.component or not coaxial(a, b):
                continue
            pair = tuple(sorted((a.component, b.component)))
            line = tuple(round(v, 1) for v in _cross(a.origin, a.axis)) + tuple(round(abs(v), 3) for v in a.axis)
            key = pair + line
            fit = abs(a.radius - b.radius)
            if key not in best or fit < best[key][0]:
                best[key] = (fit, a, b)
    return [(a, b) for _, a, b in best.values()]


# ---------------------------------------------------------------- reading the assembly
def require_assembly(doc: Any) -> Any:
    if try_call(doc, "GetType") != 2:
        raise SwError(Code.WRONG_DOC_TYPE, "This needs an assembly to be the active window.",
                      'Open or activate the assembly first, e.g. manage_documents(action="activate", name="V4 engine").')
    return doc


def components(asm: Any) -> list[Any]:
    return [c for c in as_list(try_call(asm, "GetComponents", True)) if c is not None]


def comp_name(comp: Any) -> str:
    return str(try_call(comp, "Name2") or "")


def comp_transform(comp: Any) -> list[float]:
    data = as_list(try_call(try_call(comp, "Transform2"), "ArrayData"))
    return [float(v) for v in data] if len(data) >= 13 else list(IDENTITY)


def comp_box(comp: Any) -> tuple[list[float], list[float]] | None:
    b = as_list(try_call(comp, "GetBox", False, False))
    return (_m(b[0:3]), _m(b[3:6])) if len(b) >= 6 else None


def find_component(asm: Any, name: str) -> Any:
    wanted = name.strip().lower()
    comps = components(asm)
    for c in comps:
        n = comp_name(c).lower()
        if n == wanted or n.rsplit("-", 1)[0] == wanted:
            return c
    raise SwError(Code.NOT_FOUND, f"No part called '{name}' in this assembly.",
                  "Parts: " + ", ".join(comp_name(c) for c in comps))


def _bodies(comp: Any) -> list[Any]:
    try:
        res = call(comp, "GetBodies3", 0, win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None))
    except Exception:  # noqa: BLE001 - older API
        res = try_call(comp, "GetBodies2", 0)
    if isinstance(res, tuple) and len(res) == 2 and not hasattr(res[0], "GetFaces"):
        res = res[0]  # (bodies, out info)
    return [b for b in as_list(res) if b is not None]


def cylinder_faces(comp: Any) -> list[CylFace]:
    name = comp_name(comp)
    xf = comp_transform(comp)
    out = []
    for body in _bodies(comp):
        for face in as_list(try_call(body, "GetFaces")):
            surf = try_call(face, "GetSurface")
            if try_call(surf, "IsCylinder") is not True:
                continue
            params = as_list(try_call(surf, "CylinderParams"))
            if len(params) < 7:
                continue
            origin = apply_transform(xf, [float(v) for v in params[0:3]])
            axis_pt = apply_transform(xf, [float(params[i]) + float(params[i + 3]) for i in range(3)])
            axis = _unit(_sub(axis_pt, origin))
            origin_mm = [v * 1000 for v in origin]
            box = as_list(try_call(face, "GetBox"))
            lo, hi = -1e9, 1e9
            if len(box) >= 6:
                corners = [[float(box[i]), float(box[j]), float(box[k])] for i in (0, 3) for j in (1, 4) for k in (2, 5)]
                along = [_dot(_sub([v * 1000 for v in apply_transform(xf, p)], origin_mm), axis) for p in corners]
                lo, hi = min(along), max(along)
            out.append(CylFace(name, face, origin_mm, axis, float(params[6]) * 1000, lo, hi))
    return out


# ---------------------------------------------------------------- selecting and mating
def _select(doc: Any, entities: list[Any], mark: int) -> bool:
    call(doc, "ClearSelection2", True)
    sel = try_call(doc, "SelectionManager")
    data = try_call(sel, "CreateSelectData")
    if data is not None:
        try:
            data.Mark = mark
        except Exception:  # noqa: BLE001
            pass
    ok = True
    for ent in entities:
        try:
            ok = bool(call(ent, "Select4", True, data if data is not None else null_dispatch())) and ok
        except Exception:  # noqa: BLE001 - try the extension's multi-select below
            ok = False
            break
    if ok:
        return True
    call(doc, "ClearSelection2", True)
    arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                                  [getattr(e, "_oleobj_", e) for e in entities])
    count = try_call(call(doc, "Extension"), "MultiSelect2", arr, False, data if data is not None else null_dispatch())
    return bool(count) and int(count) >= len(entities)


def _add_concentric(doc: Any, a: CylFace, b: CylFace) -> Any:
    if not _select(doc, [a.face, b.face], 1):
        return None
    err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    mate = try_call(doc, "AddMate5", MATE_CONCENTRIC, ALIGN_CLOSEST, False,
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False, False, 0, err)
    if isinstance(mate, tuple):
        mate = mate[0]
    call(doc, "ClearSelection2", True)
    return mate


def _set_fixed(doc: Any, comp: Any, fixed: bool) -> None:
    call(doc, "ClearSelection2", True)
    if try_call(comp, "Select4", False, null_dispatch(), False):
        try_call(doc, "FixComponent" if fixed else "UnfixComponent")
    call(doc, "ClearSelection2", True)


def _box_volume(box) -> float:
    if not box:
        return 0.0
    lo, hi = box
    return max(0.0, hi[0] - lo[0]) * max(0.0, hi[1] - lo[1]) * max(0.0, hi[2] - lo[2])


def connect_parts(doc: Any, fixed_part: str = "") -> dict:
    asm = require_assembly(doc)
    comps = components(asm)
    if len(comps) < 2:
        raise SwError(Code.BAD_ARGUMENT, "The assembly needs at least two parts to connect.", "Make the assembly first.")
    by_name = {comp_name(c): c for c in comps}
    base = find_component(asm, fixed_part) if fixed_part.strip() else max(comps, key=lambda c: _box_volume(comp_box(c)))
    base_name = comp_name(base)
    boxes_before = {n: comp_box(c) for n, c in by_name.items()}

    faces = [f for c in comps for f in cylinder_faces(c)]
    joints = find_joints(faces)
    linked: dict[str, set[str]] = {n: set() for n in by_name}
    made, failed = [], []
    for a, b in joints:
        mate = _add_concentric(asm, a, b)
        label = f"{a.component} + {b.component} (d{2 * min(a.radius, b.radius):g} mm)"
        if mate is None:
            failed.append(label)
            continue
        made.append(label)
        linked[a.component].add(b.component)
        linked[b.component].add(a.component)

    # the base is fixed; parts not connected to anything are fixed too, so they stay where designed
    fixed = [base_name] + [n for n, links in linked.items() if not links and n != base_name]
    for n, comp in by_name.items():
        _set_fixed(asm, comp, n in fixed)
    try_call(asm, "EditRebuild3")

    moved = []
    for n, c in by_name.items():
        before, after = boxes_before.get(n), comp_box(c)
        if before and after and any(abs(before[k][i] - after[k][i]) > 0.05 for k in range(2) for i in range(3)):
            moved.append(n)
    out: dict[str, Any] = {"fixed_parts": fixed, "joints": made}
    moving = [n for n, links in linked.items() if links and n != base_name]
    if moving:
        out["can_move"] = moving
    if failed:
        out["failed_joints"] = failed
    if moved:
        out["warning"] = (f"These parts moved while connecting: {', '.join(moved)}. A shaft and bore were "
                          "probably not exactly coaxial; check those parts.")
    if not made:
        out["hint"] = ("No shaft-in-bore pairs were found. Parts connect when a cylinder of one part sits "
                       "exactly in a hole or cylinder of another (same axis, radius within 1 mm).")
    else:
        out["next"] = "Turn a moving part with move_mechanism, or make_motion_study for a SolidWorks motion study."
    return out


# ---------------------------------------------------------------- turning a part
def _joint_axis(asm: Any, comp: Any) -> CylFace:
    name = comp_name(comp)
    mine = cylinder_faces(comp)
    others = [f for c in components(asm) if comp_name(c) != name for f in cylinder_faces(c)]
    best = None
    for a in mine:
        for b in others:
            if coaxial(a, b) and (best is None or a.radius > best.radius):
                best = a
    if best is None:
        raise SwError(Code.NOT_FOUND, f"'{name}' has no shaft or bore shared with another part, so there is "
                                      "no axis to turn it around.", "Run connect_parts first, or pick another part.")
    return best


def move_mechanism(app: Any, doc: Any, part: str, degrees: float, steps: int) -> dict:
    asm = require_assembly(doc)
    comp = find_component(asm, part)
    axis = _joint_axis(asm, comp)
    start = comp_transform(comp)
    mu = call(app, "GetMathUtility")
    comps = {comp_name(c): c for c in components(asm)}
    start_centers = {n: _center(comp_box(c)) for n, c in comps.items()}
    ranges = {n: [list(p), list(p)] for n, p in start_centers.items() if p}
    for k in range(1, steps + 1):
        rot = rotation_about(axis.axis, degrees * k / steps, axis.origin)
        data = compose(start, rot)
        xf = call(mu, "CreateTransform", win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, data))
        comp.Transform2 = xf
        try_call(asm, "EditRebuild3")
        try_call(asm, "GraphicsRedraw2")
        for n, c in comps.items():
            p = _center(comp_box(c))
            if p and n in ranges:
                ranges[n][0] = [min(a, b) for a, b in zip(ranges[n][0], p)]
                ranges[n][1] = [max(a, b) for a, b in zip(ranges[n][1], p)]
        time.sleep(0.02)
    moved, still = [], []
    for n, (lo, hi) in ranges.items():
        span = [hi[i] - lo[i] for i in range(3)]
        travel = max(span)
        if travel > 0.1:
            moved.append({"part": n, "travel_mm": round(travel, 1), "mostly_along": "xyz"[span.index(travel)]})
        else:
            still.append(n)
    return {"turned": comp_name(comp), "degrees": degrees, "steps": steps,
            "axis": {"point_mm": [round(v, 2) for v in axis.origin], "direction": [round(v, 4) for v in axis.axis]},
            "moved": moved, "did_not_move": still}


def _center(box) -> list[float] | None:
    if not box:
        return None
    lo, hi = box
    return [(lo[i] + hi[i]) / 2 for i in range(3)]


# ---------------------------------------------------------------- SolidWorks Motion Study
def make_motion_study(doc: Any, part: str, rpm: float, seconds: float, kind: str) -> dict:
    """Create a Motion Study with a rotary motor on `part`. Best effort: every step is checked,
    and the members of unfamiliar API objects are logged so a failure can be fixed quickly."""
    asm = require_assembly(doc)
    comp = find_component(asm, part)
    axis = _joint_axis(asm, comp)
    msm = call(call(asm, "Extension"), "GetMotionStudyManager")
    if msm is None:
        raise SwError(Code.UNSUPPORTED, "This SolidWorks does not offer motion studies through its API.",
                      "Use move_mechanism instead.")
    study = try_call(msm, "CreateMotionStudy")
    if study is None:
        raise SwError(Code.SW_ERROR, "SolidWorks could not create a motion study.", "Use move_mechanism instead.")
    log.info("motion study members: %s", typelib.member_names(study))
    try_call(study, "Activate")
    type_names = {"animation": ("swMotionStudyTypeAssembly", "swMotionStudyTypeAnimation"),
                  "basic": ("swMotionStudyTypePhysicalSimulation", "swMotionStudyTypeBasicMotion")}[kind]
    study_type = typelib.value(*type_names)
    notes = []
    if study_type is not None:
        try:
            study.StudyType = study_type
        except Exception as exc:  # noqa: BLE001
            notes.append(f"could not set the study type ({exc})")
    for attempt in (lambda: call(study, "SetDuration", float(seconds)), lambda: setattr(study, "Duration", float(seconds))):
        try:
            attempt()
            break
        except Exception:  # noqa: BLE001
            continue

    motor_type = typelib.value("swFmAEMRotaryMotor", "swFmAEMRotationalMotor", "swFmAEMRotaryMotorFeature")
    if motor_type is None:
        found = typelib.search("motor")
        log.info("motor constants found: %s", found)
        rot = {k: v for k, v in found.items() if "rot" in k.lower()}
        motor_type = next(iter(rot.values()), None)
    if motor_type is None:
        raise SwError(Code.UNSUPPORTED, "Could not find SolidWorks' rotary-motor constant on this PC.",
                      "The study was created without a motor. Use move_mechanism to turn the part instead.")
    definition = call(study, "CreateDefinition", motor_type)
    if definition is None:
        raise SwError(Code.SW_ERROR, "SolidWorks refused to create the motor definition.", "Use move_mechanism.")
    members = typelib.member_names(definition)
    log.info("motor definition members: %s", members)
    _select(asm, [axis.face], 1)
    for setter in _motor_setters(definition, axis, rpm, members):
        try:
            setter()
        except Exception as exc:  # noqa: BLE001 - log and keep trying the other properties
            log.info("motor property failed: %s", exc)
    feature = try_call(study, "CreateFeature", definition)
    call(asm, "ClearSelection2", True)
    if feature is None:
        raise SwError(Code.SW_ERROR,
                      "The motion study was created, but SolidWorks did not accept the motor.",
                      "Tell the user: open the Motion Study tab and add a rotary motor on the "
                      f"{comp_name(comp)} by hand; the details are in the log for the developer.")
    calculated = try_call(study, "Calculate")
    try_call(study, "Play")
    out: dict[str, Any] = {"motion_study": try_call(study, "Name") or "Motion Study",
                           "motor_on": comp_name(comp), "rpm": rpm, "seconds": seconds, "type": kind,
                           "calculated": calculated is not False}
    if notes:
        out["notes"] = notes
    out["next"] = ("It plays in SolidWorks now (Motion Study tab at the bottom). To save a video: "
                   "in that tab, click 'Save Animation'.")
    return out


def _motor_setters(definition: Any, axis: CylFace, rpm: float, members: list[str]):
    """Property assignments to try on the motor definition (names differ between releases)."""
    rad_s = rpm * 2 * math.pi / 60
    yield lambda: setattr(definition, "MotorType", typelib.value("swMotorTypeRotary", "swRotaryMotor") or 0)
    if "SetDirectionReference" in members:
        yield lambda: call(definition, "SetDirectionReference", axis.face)
    if "SetLocationReference" in members:
        yield lambda: call(definition, "SetLocationReference", axis.face)
    for prop in ("ConstantSpeed", "Speed", "MotorSpeed", "Velocity"):
        if not members or prop in members:
            yield (lambda p=prop: setattr(definition, p, rad_s))
