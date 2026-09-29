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
AXIAL_GAP_MM = 1.5        # coaxial faces this close along the axis still form a joint


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
    # They overlap along the axis, or sit side by side with a small gap: a rod between the two
    # halves of a piston's pin hole (the slot splits the hole into two faces).
    return min(a.hi, b_hi) - max(a.lo, b_lo) > -AXIAL_GAP_MM


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
    _design_joints(asm, made, fixed, moving)
    if not made:
        out["hint"] = ("No shaft-in-bore pairs were found. Parts connect when a cylinder of one part sits "
                       "exactly in a hole or cylinder of another (same axis, radius within 1 mm).")
    else:
        out["next"] = "Turn a moving part with move_mechanism, or make_motion_study for a SolidWorks motion study."
    return out


def _design_project(asm: Any) -> str | None:
    from . import design

    return design.project_of_path(str(try_call(asm, "GetPathName") or ""))


def _design_joints(asm: Any, made: list[str], fixed: list[str], moving: list[str]) -> None:
    try:
        from . import design

        project = _design_project(asm)
        if project:
            design.record_joints(project, made, fixed, moving)
    except Exception:  # noqa: BLE001 - the record must never break the tool
        log.warning("could not record the joints", exc_info=True)


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


def _set_transform(mu: Any, comp: Any, data: list[float]) -> None:
    comp.Transform2 = call(mu, "CreateTransform", win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, data))


def _stem(name: str) -> str:
    return name.rsplit("-", 1)[0].lower()


def move_mechanism(app: Any, doc: Any, part: str, degrees: float, steps: int) -> dict:
    from . import engine

    asm = require_assembly(doc)
    comp = find_component(asm, part)
    mu = call(app, "GetMathUtility")
    comps = {comp_name(c): c for c in components(asm)}
    starts = {n: comp_transform(c) for n, c in comps.items()}
    centers0 = {n: _center(comp_box(c)) for n, c in comps.items()}
    farthest = {n: 0.0 for n, p in centers0.items() if p}
    spec = engine.read_motion(str(try_call(asm, "GetPathName") or ""))
    exact = spec is not None and _stem(comp_name(comp)) == spec.get("crank", "").lower()
    by_stem = {_stem(n): n for n in comps}
    axis = None if exact else _joint_axis(asm, comp)
    for k in range(1, steps + 1):
        angle = degrees * k / steps
        if exact:  # the engine's motion is known exactly: place every moving part
            for stem, motion in engine.poses(spec, angle).items():
                name = by_stem.get(stem.lower())
                if name is not None:
                    _set_transform(mu, comps[name], compose(starts[name], motion))
        else:  # turn the part and let SolidWorks' mates move the rest
            _set_transform(mu, comp, compose(starts[comp_name(comp)], rotation_about(axis.axis, angle, axis.origin)))
            try_call(asm, "EditRebuild3")
        try_call(asm, "GraphicsRedraw2")
        for n, c in comps.items():
            p = _center(comp_box(c))
            if p and n in farthest:
                farthest[n] = max(farthest[n], math.dist(p, centers0[n]))
        time.sleep(0.02)
    moved = [{"part": n, "travel_mm": round(t, 1)} for n, t in farthest.items() if t > 0.1]
    still = [n for n, t in farthest.items() if t <= 0.1]
    out: dict[str, Any] = {"turned": comp_name(comp), "degrees": degrees, "steps": steps,
                           "moved": moved, "did_not_move": still}
    try:
        from . import design

        project = _design_project(asm)
        if project:
            design.record_event(project, f"turned {comp_name(comp)} {degrees:g} deg: " + (
                ", ".join(f"{mv['part']} {mv['travel_mm']} mm" for mv in moved[:8]) or "nothing else moved"))
    except Exception:  # noqa: BLE001
        log.warning("could not record the motion", exc_info=True)
    if exact:
        out["motion"] = "exact engine motion (slider-crank): travel_mm of a piston is its stroke"
    else:
        out["axis"] = {"point_mm": [round(v, 2) for v in axis.origin], "direction": [round(v, 4) for v in axis.axis]}
    return out


def _center(box) -> list[float] | None:
    if not box:
        return None
    lo, hi = box
    return [(lo[i] + hi[i]) / 2 for i in range(3)]


# ---------------------------------------------------------------- SolidWorks Motion Study
MOTOR_INTERFACE = "ISimulationMotorFeatureData"


@dataclass
class MotorSpec:
    part: str
    kind: str          # rotary | swing | slide
    speed: float = 0.0  # rotary: rpm
    amount: float = 0.0  # swing: degrees, slide: mm
    hz: float = 0.0     # swing/slide: cycles per second
    reverse: bool = False

    def describe(self) -> str:
        if self.kind == "rotary":
            return f"{self.part}: turns at {self.speed:g} rpm"
        unit = "deg" if self.kind == "swing" else "mm"
        return (f"{self.part}: {self.kind}s {self.amount:g} {unit} back and forth, {self.hz:g} per second"
                + (", opposite direction" if self.reverse else ""))


def parse_motors(text: str) -> list[MotorSpec]:
    """Extra motors, one per line: "<part> rotary <rpm>", "<part> swing <degrees> <per second>" or
    "<part> slide <mm> <per second>", optionally followed by "reverse"."""
    out = []
    for raw in (text or "").replace(";", "\n").splitlines():
        words = raw.replace(",", " ").split()
        if not words:
            continue
        reverse = words[-1].lower() in ("reverse", "reversed", "opposite")
        if reverse:
            words = words[:-1]
        try:
            part, kind = words[0], words[1].lower()
            numbers = [float(w) for w in words[2:]]
        except (IndexError, ValueError):
            raise SwError(Code.BAD_ARGUMENT, f"Cannot read the motor line '{raw.strip()}'.",
                          'Write e.g. "pulley_a slide 10 0.5" or "fan rotary 120".') from None
        if kind == "rotary" and len(numbers) == 1 and numbers[0] > 0:
            out.append(MotorSpec(part, "rotary", speed=numbers[0], reverse=reverse))
        elif kind in ("swing", "slide") and len(numbers) == 2 and numbers[0] > 0 and numbers[1] > 0:
            out.append(MotorSpec(part, kind, amount=numbers[0], hz=numbers[1], reverse=reverse))
        else:
            raise SwError(Code.BAD_ARGUMENT, f"Cannot read the motor line '{raw.strip()}'.",
                          'Use "<part> rotary <rpm>", "<part> swing <degrees> <per second>" or '
                          '"<part> slide <mm> <per second>", optionally ending with "reverse".')
    return out


def joint_links(asm: Any) -> dict[str, set[str]]:
    """Which parts are joined to which (by coaxial shaft/bore faces)."""
    comps = components(asm)
    links: dict[str, set[str]] = {comp_name(c): set() for c in comps}
    faces = [f for c in comps for f in cylinder_faces(c)]
    for a, b in find_joints(faces):
        links[a.component].add(b.component)
        links[b.component].add(a.component)
    return links


def unlinked_parts(asm: Any, drivers: list[str]) -> list[str]:
    """Moving (not fixed) parts that no joint path connects to a driven part: they cannot move."""
    links = joint_links(asm)
    reach, todo = set(drivers), list(drivers)
    while todo:
        for other in links.get(todo.pop(), ()):
            if other not in reach:
                reach.add(other)
                todo.append(other)
    fixed = {comp_name(c) for c in components(asm) if try_call(c, "IsFixed") is True}
    return sorted(n for n in links if n not in reach and n not in fixed)


def _put(obj: Any, prop: str, value: Any) -> bool:
    """Set a property; for object values late binding may need PROPERTYPUTREF, and names that do not
    resolve are called by the DISPID from SolidWorks' type library."""
    try:
        setattr(obj, prop, value)
        return True
    except Exception as exc:  # noqa: BLE001 - try the other forms
        first = exc
    raw = getattr(value, "_oleobj_", value)
    ids = []
    try:
        ids.append(obj._oleobj_.GetIDsOfNames(prop))
    except Exception:  # noqa: BLE001
        pass
    known = typelib.member_id(MOTOR_INTERFACE, prop)
    if known is not None and known not in ids:
        ids.append(known)
    for dispid in ids:
        for kind in (pythoncom.INVOKE_PROPERTYPUTREF, pythoncom.INVOKE_PROPERTYPUT):
            try:
                obj._oleobj_.Invoke(dispid, 0, kind, 0, raw)
                return True
            except Exception:  # noqa: BLE001
                continue
    log.info("motor property %s failed: %s (dispids tried %s)", prop, first, ids)
    return False


def _invoke(obj: Any, method: str, *args: Any) -> bool:
    try:
        call(obj, method, *args)
        return True
    except Exception as exc:  # noqa: BLE001 - call it by its DISPID from the type library
        first = exc
    dispid = typelib.member_id(MOTOR_INTERFACE, method)
    if dispid is not None:
        try:
            obj._oleobj_.Invoke(dispid, 0, pythoncom.INVOKE_FUNC, True, *args)
            return True
        except Exception as exc:  # noqa: BLE001
            log.info("%s by dispid %s failed: %s", method, dispid, exc)
    log.info("%s failed: %s", method, first)
    return False


def _motor_type(kind: str) -> int | None:
    if kind == "slide":
        found = typelib.value("swFmAEMLinearMotor")
        return found if found is not None else next(iter(
            {k: v for k, v in typelib.search("motor").items() if "linear" in k.lower()}.values()), None)
    found = typelib.value("swFmAEMRotaryMotor", "swFmAEMRotationalMotor", "swFmAEMRotaryMotorFeature")
    if found is None:
        found = next(iter({k: v for k, v in typelib.search("motor").items() if "rot" in k.lower()}.values()), None)
    return found


def _add_motor(asm: Any, study: Any, spec: MotorSpec) -> str | None:
    """One motor feature, set as in the SOLIDWORKS API example (ISimulationMotorFeatureData):
    DirectionReference + Location = the part's joint face, then the motion method."""
    comp = find_component(asm, spec.part)
    axis = _joint_axis(asm, comp)
    motor_type = _motor_type(spec.kind)
    if motor_type is None:
        raise SwError(Code.UNSUPPORTED, "Could not find SolidWorks' motor constants on this PC.",
                      "Use move_mechanism to turn the part instead.")
    definition = call(study, "CreateDefinition", motor_type)
    if definition is None:
        raise SwError(Code.SW_ERROR, "SolidWorks refused to create a motor.", "Use move_mechanism instead.")
    _select(asm, [axis.face], 1)
    done = [p for p in ("DirectionReference", "Location") if _put(definition, p, axis.face)]
    if spec.reverse and _put(definition, "ReverseDirection", True):
        done.append("ReverseDirection")
    if spec.kind == "rotary":
        ok = _invoke(definition, "ConstantSpeedMotor", float(spec.speed))  # rpm
    elif spec.kind == "swing":
        ok = _invoke(definition, "OscillatingMotor", float(spec.amount), float(spec.hz))  # degrees, Hz
    else:  # slide: SolidWorks works in meters internally
        ok = _invoke(definition, "OscillatingMotor", float(spec.amount) / 1000.0, float(spec.hz))
    if ok:
        done.append(spec.kind)
    log.info("motor %s: settings accepted %s", spec.describe(), done)
    feature = try_call(study, "CreateFeature", definition)
    call(asm, "ClearSelection2", True)
    return (try_call(feature, "Name") or spec.describe()) if feature is not None else None


def make_motion_study(doc: Any, part: str, rpm: float, seconds: float, kind: str, more_motors: str = "") -> dict:
    """A Motion Study with a rotary motor on `part` (and any extra motors), calculated and played."""
    asm = require_assembly(doc)
    specs = [MotorSpec(part, "rotary", speed=rpm)] + parse_motors(more_motors)  # check the text first
    driven = [comp_name(find_component(asm, s.part)) for s in specs]
    warnings = []
    loose = unlinked_parts(asm, driven)
    if loose:
        warnings.append(f"{', '.join(loose)} {'is' if len(loose) == 1 else 'are'} not joined to "
                        f"{', '.join(driven)} and will not move. Put their shafts and bores on the same axis "
                        "(radius within 1 mm), rebuild them, and run connect_parts.")
    msm = call(call(asm, "Extension"), "GetMotionStudyManager")
    if msm is None:
        raise SwError(Code.UNSUPPORTED, "This SolidWorks does not offer motion studies through its API.",
                      "Use move_mechanism instead.")
    study = try_call(msm, "CreateMotionStudy")
    if study is None:
        raise SwError(Code.SW_ERROR, "SolidWorks could not create a motion study.", "Use move_mechanism instead.")
    try_call(study, "Activate")
    type_names = {"animation": ("swMotionStudyTypeAssembly", "swMotionStudyTypeAnimation"),
                  "basic": ("swMotionStudyTypePhysicalSimulation", "swMotionStudyTypeBasicMotion")}[kind]
    study_type = typelib.value(*type_names)
    if study_type is not None:
        try:
            study.StudyType = study_type
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"could not set the study type ({exc})")
    for attempt in (lambda: call(study, "SetDuration", float(seconds)), lambda: setattr(study, "Duration", float(seconds))):
        try:
            attempt()
            break
        except Exception:  # noqa: BLE001
            continue
    made, refused = [], []
    for spec in specs:
        name = _add_motor(asm, study, spec)
        (made if name else refused).append(spec.describe())
    if not made:
        raise SwError(Code.SW_ERROR, "The motion study was created, but SolidWorks did not accept the motor.",
                      "Tell the user: open the Motion Study tab and add a rotary motor on the "
                      f"{driven[0]} by hand; the details are in the log for the developer.")
    calculated = try_call(study, "Calculate")
    try_call(study, "Play")
    out: dict[str, Any] = {"motion_study": try_call(study, "Name") or "Motion Study", "motor_on": driven[0],
                           "motors": made, "rpm": rpm, "seconds": seconds, "type": kind,
                           "calculated": calculated is not False}
    if refused:
        warnings.append("SolidWorks did not accept these motors: " + "; ".join(refused))
    if warnings:
        out["warnings"] = warnings
    out["next"] = ("It plays in SolidWorks now (Motion Study tab at the bottom). To save a video: "
                   "in that tab, click 'Save Animation'.")
    return out
