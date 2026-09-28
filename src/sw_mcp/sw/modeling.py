"""Build parts from simple primitives given in WORLD coordinates (millimeters).

World frame (the SolidWorks default part template): X to the right, Y up, Z toward the viewer.
Default planes: Front = XY (normal +Z), Top = XZ (normal +Y), Right = YZ (normal +X).

Every primitive is a 2D profile drawn on the default plane perpendicular to its `axis`, then
extruded between two world coordinates along that axis (added or cut). SolidWorks' direction
flags and sketch-axis orientation are easy to get backwards, so every result is VERIFIED from
the faces the new feature actually created. A wrongly placed attempt is deleted and retried
with the other direction, so the LLM only ever deals with plain world coordinates.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import pythoncom
import win32com.client

from ..core.com_utils import as_list, call, call_with_out_ints, null_dispatch, try_call
from ..core.errors import Code, SwError

log = logging.getLogger(__name__)

AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
# The two in-plane world axes for each extrusion axis, in alphabetical order.
IN_PLANE = {"x": (1, 2), "y": (0, 2), "z": (0, 1)}
IN_PLANE_NAMES = {"x": "(y,z)", "y": "(x,z)", "z": "(x,y)"}
# Default planes in feature-tree order: Front, Top, Right (works in every UI language).
PLANE_INDEX = {"z": 0, "y": 1, "x": 2}
# Sketch coordinates for a world point on each default plane, when SolidWorks' own
# transform cannot be read. Verified results make a wrong guess self-correcting.
FALLBACK_TO_SKETCH = {
    "z": lambda p: (p[0], p[1]),
    "y": lambda p: (p[0], -p[2]),
    "x": lambda p: (-p[2], p[1]),
}

TOL_MM = 0.05
MAX_SIZE_MM = 100_000.0
# InsertMoveCopyBody2's three rotation angles, as slot index per axis. Measured on SOLIDWORKS 2024:
# the slots turn about Z, Y, X (asking X turned about Z). The documented order is the fallback.
ANGLE_SLOTS = ({"x": 2, "y": 1, "z": 0}, {"x": 0, "y": 1, "z": 2})
SYSTEM_FEATURE_TYPES = {
    "CommentsFolder", "FavoriteFolder", "HistoryFolder", "SelectionSetFolder", "SensorFolder",
    "LiveSectionFolder", "DocsFolder", "DetailCabinet", "EnvFolder", "InkMarkupFolder", "EqnFolder",
    "MaterialFolder", "RefPlane", "OriginProfileFeature", "MateGroup", "Reference", "SolidBodyFolder",
    "SurfaceBodyFolder", "BlockFolder", "MagneticGroundPlane", "ProfileFeature",
}


# ============================================================ pure geometry (unit-tested)
@dataclass
class Profile:
    kind: str  # "polygon" | "circle"
    points: list[tuple[float, float]] = field(default_factory=list)  # mm, in-plane (a, b)
    radius: float = 0.0  # mm, circles only

    def bounds(self) -> tuple[float, float, float, float]:
        if self.kind == "circle":
            (a, b), r = self.points[0], self.radius
            return a - r, a + r, b - r, b + r
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return min(xs), max(xs), min(ys), max(ys)

    def area(self) -> float:
        if self.kind == "circle":
            return math.pi * self.radius**2
        pts = self.points
        twice = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                    for i in range(len(pts)))
        return abs(twice) / 2.0


@dataclass
class Shape:
    axis: str
    profile: Profile
    start: float  # mm along axis, start < end
    end: float
    cut: bool
    label: str
    # Optional rotation applied after building: (axis "x|y|z", degrees, pivot point in mm).
    tilt: tuple[str, float, tuple[float, float, float]] | None = None

    def world_box(self) -> tuple[list[float], list[float]]:
        a0, a1, b0, b1 = self.profile.bounds()
        lo, hi = [0.0] * 3, [0.0] * 3
        ia, ib = IN_PLANE[self.axis]
        n = AXIS_INDEX[self.axis]
        lo[ia], hi[ia], lo[ib], hi[ib] = a0, a1, b0, b1
        lo[n], hi[n] = self.start, self.end
        return lo, hi

    def world_point(self, a: float, b: float, along: float = 0.0) -> list[float]:
        p = [0.0, 0.0, 0.0]
        ia, ib = IN_PLANE[self.axis]
        p[ia], p[ib], p[AXIS_INDEX[self.axis]] = a, b, along
        return p

    def volume(self) -> float:
        return self.profile.area() * (self.end - self.start)


def _bad(message: str, fix: str) -> SwError:
    return SwError(Code.BAD_ARGUMENT, message, fix)


def _check_range(name: str, lo: float, hi: float) -> None:
    for v in (lo, hi):
        if not math.isfinite(v) or abs(v) > MAX_SIZE_MM:
            raise _bad(f"{name} value {v} is out of range.", "Use millimeters between -100000 and 100000.")
    if hi - lo < 0.001:
        raise _bad(
            f"{name}: the max must be larger than the min (got {lo} and {hi}).",
            "Give two different values; the order does not matter for the tool, but they must differ.",
        )


def box_shape(x_min: float, x_max: float, y_min: float, y_max: float, z_min: float, z_max: float,
              cut: bool) -> Shape:
    x_min, x_max = sorted((x_min, x_max))
    y_min, y_max = sorted((y_min, y_max))
    z_min, z_max = sorted((z_min, z_max))
    _check_range("x", x_min, x_max)
    _check_range("y", y_min, y_max)
    _check_range("z", z_min, z_max)
    rect = [(x_min, z_min), (x_max, z_min), (x_max, z_max), (x_min, z_max)]
    return Shape("y", Profile("polygon", rect), y_min, y_max, cut, "Pocket" if cut else "Box")


def cylinder_shape(x1: float, y1: float, z1: float, x2: float, y2: float, z2: float,
                   diameter: float, cut: bool) -> Shape:
    p1, p2 = (x1, y1, z1), (x2, y2, z2)
    differ = [i for i in range(3) if abs(p1[i] - p2[i]) > 1e-6]
    if not (0.001 <= diameter <= MAX_SIZE_MM):
        raise _bad(f"diameter {diameter} is out of range.", "Use a positive diameter in millimeters.")
    if not differ:
        raise _bad("The cylinder's start and end are the same point.",
                   "The end must be the other end of the cylinder, e.g. start (0,0,0) and end (0,50,0).")
    if len(differ) == 3:
        raise _bad(
            "A slanted cylinder must keep one coordinate the same at both ends (it lies in a plane "
            "parallel to Front, Top or Right).",
            "Example of a 45 degree bore in the Y-Z plane: start (0, 0, 0) and end (0, 70.7, 70.7).",
        )
    if len(differ) == 2:
        return _slanted_cylinder(p1, p2, diameter, cut)
    n = differ[0]
    axis = "xyz"[n]
    ia, ib = IN_PLANE[axis]
    start, end = sorted((p1[n], p2[n]))
    _check_range(axis, start, end)
    prof = Profile("circle", [(p1[ia], p1[ib])], diameter / 2.0)
    return Shape(axis, prof, start, end, cut, "Hole" if cut else "Cylinder")


def _slanted_cylinder(p1: tuple, p2: tuple, diameter: float, cut: bool) -> Shape:
    """A cylinder from p1 to p2 lying in a plane parallel to a default plane: built straight
    along a base axis from p1, then tilted about p1 into the requested direction."""
    d = [p2[i] - p1[i] for i in range(3)]
    k = next(i for i in range(3) if abs(d[i]) <= 1e-6)  # the tilt axis (the unchanged coordinate)
    b, o = (k + 1) % 3, (k + 2) % 3  # base axis x->y, y->z, z->x keeps the right-hand rule simple
    length = math.sqrt(sum(c * c for c in d))
    base = "xyz"[b]
    ia, ib = IN_PLANE[base]
    _check_range(base, p1[b], p1[b] + length)
    prof = Profile("circle", [(p1[ia], p1[ib])], diameter / 2.0)
    deg = math.degrees(math.atan2(d[o], d[b]))
    return Shape(base, prof, p1[b], p1[b] + length, cut, "Hole" if cut else "Cylinder",
                 ("xyz"[k], deg, (float(p1[0]), float(p1[1]), float(p1[2]))))


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def parse_points(text: str) -> list[tuple[float, float]]:
    nums = [float(n) for n in _NUMBER.findall(text or "")]
    if len(nums) % 2:
        raise _bad(f"points_mm has an odd count of numbers ({len(nums)}).",
                   'Give pairs like "0,0; 40,0; 40,10; 0,10".')
    pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) < 1e-6:
        pts.pop()  # a repeated closing point is fine
    dedup = [p for i, p in enumerate(pts) if i == 0 or math.dist(p, pts[i - 1]) > 1e-6]
    if len(dedup) < 3:
        raise _bad("points_mm needs at least 3 different corner points.",
                   'Give the corners in order around the shape, e.g. "0,0; 40,0; 40,10; 0,10".')
    if len(dedup) > 64:
        raise _bad("points_mm has more than 64 points.", "Simplify the outline.")
    return dedup


def prism_shape(axis: str, points_mm: str, start: float, end: float, cut: bool) -> Shape:
    pts = parse_points(points_mm)
    prof = Profile("polygon", pts)
    if prof.area() < 1e-6:
        raise _bad("The points do not enclose an area (they are all on one line).",
                   "List the corners in order around the outline.")
    for a, b in pts:
        if abs(a) > MAX_SIZE_MM or abs(b) > MAX_SIZE_MM:
            raise _bad("A point is out of range.", "Use millimeters between -100000 and 100000.")
    start, end = sorted((start, end))
    _check_range(axis, start, end)
    return Shape(axis, prof, start, end, cut, "Cut-Prism" if cut else "Prism")


def translated(shape: Shape, dx: float, dy: float, dz: float) -> Shape:
    d = (dx, dy, dz)
    ia, ib = IN_PLANE[shape.axis]
    n = AXIS_INDEX[shape.axis]
    pts = [(a + d[ia], b + d[ib]) for a, b in shape.profile.points]
    prof = Profile(shape.profile.kind, pts, shape.profile.radius)
    tilt = None
    if shape.tilt:
        axis, deg, about = shape.tilt
        tilt = (axis, deg, (about[0] + dx, about[1] + dy, about[2] + dz))
    return Shape(shape.axis, prof, shape.start + d[n], shape.end + d[n], shape.cut, shape.label, tilt)


def rotated(shape: Shape, angle_deg: float, center: tuple[float, float, float]) -> Shape:
    """Rotate a shape about a line parallel to its own axis through `center` (right-hand rule
    about the +axis direction). The profile turns in its plane; the extent along the axis stays."""
    if shape.tilt:
        raise _bad("A tilted shape cannot be repeated around a circle.",
                   "Repeat it in a row instead, or write each tilted copy as its own step.")
    ia, ib = IN_PLANE[shape.axis]
    ca, cb = center[ia], center[ib]
    # In-plane (a, b) are (y,z) for x and (x,y) for z: right-handed. For y they are (x,z),
    # which is left-handed about +Y, so the angle flips sign.
    t = math.radians(-angle_deg if shape.axis == "y" else angle_deg)
    cos, sin = math.cos(t), math.sin(t)
    pts = [(ca + (a - ca) * cos - (b - cb) * sin, cb + (a - ca) * sin + (b - cb) * cos)
           for a, b in shape.profile.points]
    prof = Profile(shape.profile.kind, pts, shape.profile.radius)
    return Shape(shape.axis, prof, shape.start, shape.end, shape.cut, shape.label)


def rotate_point(p: list[float] | tuple, axis: str, deg: float, about: tuple) -> list[float]:
    """Right-hand rotation of a world point about a line parallel to `axis` through `about`."""
    t = math.radians(deg)
    c, s_ = math.cos(t), math.sin(t)
    x, y, z = (p[0] - about[0], p[1] - about[1], p[2] - about[2])
    if axis == "x":
        y, z = y * c - z * s_, y * s_ + z * c
    elif axis == "y":
        x, z = x * c + z * s_, -x * s_ + z * c
    else:
        x, y = x * c - y * s_, x * s_ + y * c
    return [x + about[0], y + about[1], z + about[2]]


def surface_points(shape: Shape) -> list[list[float]]:
    """Points on the shape's outline at both ends (rims sampled for circles)."""
    prof = shape.profile
    if prof.kind == "circle":
        (a, b), r = prof.points[0], prof.radius
        ring = [(a + r * math.cos(k * math.pi / 36), b + r * math.sin(k * math.pi / 36)) for k in range(72)]
    else:
        ring = prof.points
    return [shape.world_point(a, b, along) for along in (shape.start, shape.end) for a, b in ring]


def tilted_box(shape: Shape) -> tuple[list[float], list[float]]:
    axis, deg, about = shape.tilt  # type: ignore[misc]
    pts = [rotate_point(p, axis, deg, about) for p in surface_points(shape)]
    return [min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]


def boxes_match(a: tuple[list[float], list[float]], b: tuple[list[float], list[float]], tol: float = 0.5) -> bool:
    return all(abs(a[k][i] - b[k][i]) <= tol + 0.01 * abs(b[1][i] - b[0][i]) for k in range(2) for i in range(3))


@dataclass
class Revolve:
    axis: str
    center: tuple[float, float, float]
    profile: list[tuple[float, float]]  # (radius from the axis, position along the axis), mm
    angle: float
    cut: bool
    label: str

    def world(self, r: float, h: float, radial: int) -> list[float]:
        p = list(self.center)
        p[AXIS_INDEX[self.axis]] = h
        p[radial] += r
        return p

    def box(self) -> tuple[list[float], list[float]]:
        rmax = max(r for r, _ in self.profile)
        hs = [h for _, h in self.profile]
        lo, hi = [c - rmax for c in self.center], [c + rmax for c in self.center]
        n = AXIS_INDEX[self.axis]
        lo[n], hi[n] = min(hs), max(hs)
        return lo, hi

    def sketch_plane(self) -> tuple[str, int]:
        """(normal axis of the default plane that contains the revolve axis, radial world index)."""
        options = {"y": (("z", 0), ("x", 2)), "x": (("z", 1), ("y", 2)), "z": (("y", 0), ("x", 1))}
        for normal, radial in options[self.axis]:
            if abs(self.center[AXIS_INDEX[normal]]) < 1e-6:
                return normal, radial
        raise _bad(f"A revolve around {self.axis.upper()} here needs its axis on a default plane.",
                   "Use shifted() first.")

    def shifted(self) -> tuple["Revolve", list[float]]:
        """The same revolve moved so its axis lies on a default plane, and the offset (mm) that
        moves it back. SolidWorks can only sketch on the default planes here, so an off-plane
        revolve is built there as a separate body and then moved into place."""
        try:
            self.sketch_plane()
            return self, [0.0, 0.0, 0.0]
        except SwError:
            pass
        n = AXIS_INDEX[{"y": "z", "x": "z", "z": "y"}[self.axis]]
        center = list(self.center)
        offset = [0.0, 0.0, 0.0]
        offset[n], center[n] = center[n], 0.0
        return Revolve(self.axis, tuple(center), self.profile, self.angle, self.cut, self.label), offset


def revolve_spec(axis: str, center: tuple, profile: list[tuple[float, float]], angle: float, cut: bool) -> Revolve:
    pts = [(float(r), float(h)) for r, h in profile]
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) < 1e-6:
        pts.pop()
    if len(pts) < 3:
        raise _bad("A revolve profile needs at least 3 points (radius, position along the axis).",
                   'Example for a disc 40 mm wide and 10 thick: [[0,0],[20,0],[20,10],[0,10]].')
    if any(r < -1e-9 for r, _ in pts):
        raise _bad("Revolve profile radii must be 0 or more (the profile must stay on one side of the axis).",
                   "Use the distance from the axis as the first number of each point.")
    if Profile("polygon", pts).area() < 1e-6:
        raise _bad("The revolve profile does not enclose an area.", "List the corners in order around the outline.")
    if not 0 < angle <= 360:
        raise _bad("The revolve angle must be between 0 and 360 degrees.", "Use 360 for a full round part.")
    return Revolve(axis, tuple(float(c) for c in center), pts, float(angle), cut,
                   "Cut-Revolve" if cut else "Revolve")


def placement_ok(shape: Shape, face_boxes_mm: list[tuple[list[float], list[float]]], tol: float = TOL_MM) -> bool:
    """True when most faces of the new feature sit inside the requested region.

    A feature built in the wrong direction, or mirrored, has its faces outside that region.
    Face CENTERS are used, because a face merged with an older coplanar face can extend past it.
    """
    if not face_boxes_mm:
        return False
    lo, hi = shape.world_box() if isinstance(shape, Shape) else shape
    inside = 0
    for fmin, fmax in face_boxes_mm:
        center = [(fmin[i] + fmax[i]) / 2 for i in range(3)]
        if all(lo[i] - tol <= center[i] <= hi[i] + tol for i in range(3)):
            inside += 1
    if inside >= max(1, math.ceil(len(face_boxes_mm) / 2)):
        return True
    # A flush end cap merges with the coplanar side of an older feature (a pin boss flush with a
    # rod's sides), so its face is much bigger than the new shape. Still correct if every face at
    # least reaches into the requested region and one sits inside it; a shape built the wrong way
    # has a face (its far end) completely outside the region.
    reaches = all(all(fmin[i] <= hi[i] + tol and fmax[i] >= lo[i] - tol for i in range(3))
                  for fmin, fmax in face_boxes_mm)
    return reaches and inside >= 1


@dataclass
class EdgeInfo:
    start: list[float]  # mm
    end: list[float]
    mid: list[float]
    is_line: bool
    is_circle: bool


EDGE_FILTERS = ("all", "vertical", "top", "bottom", "parallel_x", "parallel_z", "circular")


def edge_matches(edge: EdgeInfo, which: str, part_min: list[float], part_max: list[float],
                 tol: float = TOL_MM) -> bool:
    if which == "all":
        return True
    if which == "circular":
        return edge.is_circle
    pts = (edge.start, edge.end, edge.mid)
    if which == "top":
        return all(p[1] >= part_max[1] - tol for p in pts)
    if which == "bottom":
        return all(p[1] <= part_min[1] + tol for p in pts)
    if not edge.is_line:
        return False
    d = [edge.end[i] - edge.start[i] for i in range(3)]
    length = math.sqrt(sum(c * c for c in d)) or 1.0
    idx = {"vertical": 1, "parallel_x": 0, "parallel_z": 2}[which]
    return abs(d[idx]) / length > 0.999


# ============================================================ COM helpers
# Per document: the last shape built, or the last repeated group, for the repeat tools
# (kept in server memory).
_last_group: dict[str, list[Shape]] = {}
MAX_COPIES = 50


def _doc_key(doc: Any) -> str:
    return str(try_call(doc, "GetPathName") or try_call(doc, "GetTitle") or "")


def _m(values: Any) -> list[float]:
    return [float(v) * 1000.0 for v in as_list(values)[:3]]


def _r(values: list[float], nd: int = 3) -> list[float]:
    return [0.0 if round(v, nd) == 0 else round(v, nd) for v in values]


class Modeler:
    """COM side. One instance per tool call, on the COM worker thread."""

    def __init__(self, app: Any, doc: Any) -> None:
        self.app = app
        self.doc = doc
        self.title = try_call(doc, "GetTitle")

    def ensure_active(self) -> None:
        """Sketches and features go to the ACTIVE window. If the user clicked another part while a
        build runs, bring ours back first."""
        active = try_call(self.app, "ActiveDoc")
        if not self.title or (active is not None and try_call(active, "GetTitle") == self.title):
            return
        log.info("active window changed to %s; switching back to %s", try_call(active, "GetTitle"), self.title)
        try:
            call_with_out_ints(self.app, "ActivateDoc3", self.title, False, 0, n_out=1)
        except Exception:  # noqa: BLE001 - older API
            try_call(self.app, "ActivateDoc", self.title)

    # ---------------------------------------------------------------- reading the part
    def bodies(self) -> list:
        return as_list(try_call(self.doc, "GetBodies2", 0, True))  # swSolidBody, visible only

    def volume_mm3(self) -> float:
        mp = try_call(try_call(self.doc, "Extension"), "CreateMassProperty")
        vol = try_call(mp, "Volume")
        if vol is None:
            try:
                props, _ = call_with_out_ints(self.doc, "GetMassProperties2", n_out=1)
                vol = as_list(props)[3]
            except Exception:  # noqa: BLE001 - an empty part has no mass properties
                vol = 0.0
        return float(vol or 0.0) * 1e9

    def bbox_mm(self) -> tuple[list[float], list[float]] | None:
        box = as_list(try_call(self.doc, "GetPartBox", True))
        if len(box) >= 6:
            return _m(box[0:3]), _m(box[3:6])
        lo, hi = None, None
        for body in self.bodies():
            b = as_list(try_call(body, "GetBodyBox"))
            if len(b) < 6:
                continue
            bl, bh = _m(b[0:3]), _m(b[3:6])
            lo = bl if lo is None else [min(lo[i], bl[i]) for i in range(3)]
            hi = bh if hi is None else [max(hi[i], bh[i]) for i in range(3)]
        return (lo, hi) if lo is not None else None

    def summary(self) -> dict:
        out: dict[str, Any] = {}
        box = self.bbox_mm()
        if box:
            lo, hi = box
            out["size_mm"] = _r([hi[i] - lo[i] for i in range(3)], 2)
            out["min_mm"] = _r(lo, 2)
            out["max_mm"] = _r(hi, 2)
        out["volume_mm3"] = round(self.volume_mm3(), 1)
        n = len(self.bodies())
        out["bodies"] = n
        if n > 1:
            out["warning"] = (f"The part has {n} separate solid bodies: some shapes do not touch each other. "
                              "Usually a coordinate is wrong; check the last step.")
        return out

    def features(self, limit: int = 40) -> list[dict]:
        out = []
        feat = try_call(self.doc, "FirstFeature")
        n = 0
        while feat is not None and n < 2000 and len(out) < limit:
            kind = try_call(feat, "GetTypeName2") or ""
            if kind not in SYSTEM_FEATURE_TYPES and not kind.endswith("Folder"):
                out.append({"name": try_call(feat, "Name"), "type": kind})
            feat = try_call(feat, "GetNextFeature")
            n += 1
        return out

    def last_feature(self) -> Any:
        return try_call(self.doc, "FeatureByPositionReverse", 0)

    # ---------------------------------------------------------------- editing helpers
    def exit_sketch(self) -> None:
        sm = call(self.doc, "SketchManager")
        if try_call(sm, "ActiveSketch") is not None:
            call(sm, "InsertSketch", True)

    def select_only(self, obj: Any) -> None:
        call(self.doc, "ClearSelection2", True)
        if not call(obj, "Select2", False, 0):
            raise SwError(Code.SW_ERROR, "SolidWorks refused to select an item it just created.",
                          "Call get_status, then try the step again.")

    def delete(self, feature: Any) -> None:
        if feature is None:
            return
        try:
            self.select_only(feature)
            call(call(self.doc, "Extension"), "DeleteSelection2", 1)  # swDelete_Absorbed: also its sketch
        except Exception:  # noqa: BLE001 - cleanup is best effort
            log.warning("could not delete a failed attempt", exc_info=True)

    def default_planes(self) -> list:
        planes = []
        feat = try_call(self.doc, "FirstFeature")
        n = 0
        while feat is not None and n < 200 and len(planes) < 3:
            if try_call(feat, "GetTypeName2") == "RefPlane":
                planes.append(feat)
            feat = try_call(feat, "GetNextFeature")
            n += 1
        if len(planes) < 3:
            raise SwError(Code.SW_ERROR, "Could not find the Front, Top and Right planes of this part.",
                          "Create the part with new_part, which uses the default template.")
        return planes

    def unique_name(self, base: str) -> str:
        i = 1
        while try_call(self.doc, "FeatureByName", f"{base}{i}") is not None:
            i += 1
        return f"{base}{i}"

    def zoom(self) -> None:
        try_call(self.doc, "ViewZoomtofit2")

    # ---------------------------------------------------------------- sketch + extrude
    def _mapper(self, sketch: Any, axis: str, mirror: bool) -> tuple[Callable[[list[float]], tuple[float, float]], str]:
        """world point (meters) -> sketch (u, v) in meters, using SolidWorks' own transform."""
        try:
            xform = call(sketch, "ModelToSketchTransform")
            mu = call(self.app, "GetMathUtility")

            def via_transform(p: list[float]) -> tuple[float, float]:
                arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in p])
                q = call(call(call(mu, "CreatePoint", arr), "MultiplyTransform", xform), "ArrayData")
                q = as_list(q)
                if abs(q[2]) > 1e-7:
                    raise ValueError("point is not on the sketch plane")
                return float(q[0]), float(q[1])

            via_transform(_probe(axis))  # fails fast if the transform cannot be used
            if not mirror:
                return via_transform, "transform"
        except Exception:  # noqa: BLE001 - fall back to the known default-plane orientation
            log.info("sketch transform unavailable; using the default-plane table", exc_info=True)
        table = FALLBACK_TO_SKETCH[axis]
        if mirror:
            return (lambda p: (table(p)[0], -table(p)[1])), "table-mirrored"
        return table, "table"

    def _draw(self, plane_axis: str, mirror: bool, polygon: list[list[float]] | None = None,
              circle: tuple[list[float], float] | None = None,
              centerline: tuple[list[float], list[float]] | None = None) -> tuple[Any, str]:
        """Draw on the default plane whose normal is `plane_axis`. Points are world mm."""
        self.ensure_active()
        self.exit_sketch()
        plane = self.default_planes()[PLANE_INDEX[plane_axis]]
        self.select_only(plane)
        sm = call(self.doc, "SketchManager")
        call(sm, "InsertSketch", True)
        sketch = try_call(sm, "ActiveSketch")
        if sketch is None:
            raise SwError(Code.SW_ERROR, "SolidWorks did not open a sketch on the default plane.",
                          "Call get_status, then try again.")
        to_sketch, how = self._mapper(sketch, plane_axis, mirror)

        def uv(p: list[float]) -> tuple[float, float]:
            return to_sketch([c / 1000 for c in p])

        try:
            sm.AddToDB = True  # exact coordinates, no snapping to nearby geometry
        except Exception:  # noqa: BLE001
            pass
        failed = False
        try:
            if circle is not None:
                u, v = uv(circle[0])
                if call(sm, "CreateCircleByRadius", u, v, 0.0, circle[1] / 1000) is None:
                    raise SwError(Code.SW_ERROR, "SolidWorks could not draw the circle.", "Check the diameter.")
            if polygon is not None:
                pts = [uv(p) for p in polygon]
                for i, (u1, v1) in enumerate(pts):
                    u2, v2 = pts[(i + 1) % len(pts)]
                    if call(sm, "CreateLine", u1, v1, 0.0, u2, v2, 0.0) is None:
                        raise SwError(Code.SW_ERROR, "SolidWorks could not draw the outline.",
                                      "Check that the points do not repeat or cross.")
            if centerline is not None:
                (u1, v1), (u2, v2) = uv(centerline[0]), uv(centerline[1])
                if call(sm, "CreateCenterLine", u1, v1, 0.0, u2, v2, 0.0) is None:
                    raise SwError(Code.SW_ERROR, "SolidWorks could not draw the revolve axis.", "Try again.")
        except BaseException:
            failed = True
            raise
        finally:
            try:
                sm.AddToDB = False
            except Exception:  # noqa: BLE001
                pass
            call(sm, "InsertSketch", True)  # close the sketch
            if failed:
                self.delete(self.last_feature())  # do not leave a half-drawn sketch behind
        return self.last_feature(), how

    def _sketch(self, shape: Shape, mirror: bool) -> tuple[Any, str]:
        prof = shape.profile
        if prof.kind == "circle":
            return self._draw(shape.axis, mirror, circle=(shape.world_point(*prof.points[0]), prof.radius))
        return self._draw(shape.axis, mirror, polygon=[shape.world_point(a, b) for a, b in prof.points])

    def _extrude(self, shape: Shape, sketch_feat: Any, reverse: bool, flip_offset: bool, merge: bool = True) -> Any:
        self.select_only(sketch_feat)
        fm = call(self.doc, "FeatureManager")
        depth = (shape.end - shape.start) / 1000
        at_plane = abs(shape.start) < 1e-6
        t0, offset = (0, 0.0) if at_plane else (3, abs(shape.start) / 1000)  # swStartSketchPlane / swStartOffset
        f, z = False, 0.0
        if shape.cut:
            return call(fm, "FeatureCut4",
                        True, f, reverse, 0, 0, depth, 0.0, f, f, f, f, z, z, f, f, f, f,
                        f, True, True, f, f, f, t0, offset, flip_offset, f)
        return call(fm, "FeatureExtrusion3",
                    True, f, reverse, 0, 0, depth, 0.0, f, f, f, f, z, z, f, f, f, f,
                    merge, True, True, t0, offset, flip_offset)

    def _face_boxes(self, feature: Any) -> list[tuple[list[float], list[float]]]:
        boxes = []
        for face in as_list(try_call(feature, "GetFaces")):
            b = as_list(try_call(face, "GetBox"))
            if len(b) >= 6:
                boxes.append((_m(b[0:3]), _m(b[3:6])))
        return boxes

    def _place(self, shape: Shape, merge: bool = True) -> Any:
        """Sketch + extrude until the result sits where asked (see module docstring)."""
        at_plane = abs(shape.start) < 1e-6
        guess_flip = shape.start < 0
        # Measured on SOLIDWORKS 2024: a boss goes along +normal with reverse=False, while a cut
        # needs reverse=True for the same direction. Other combinations stay as fallbacks.
        guess_reverse = shape.cut
        attempts = [(guess_reverse, guess_flip), (not guess_reverse, guess_flip)]
        if not at_plane:
            attempts += [(guess_reverse, not guess_flip), (not guess_reverse, not guess_flip)]
        tried: list[str] = []
        exact_transform = False
        for mirror in (False, True):
            if mirror and exact_transform:
                break  # SolidWorks' own transform is exact; mirroring only helps the fallback table
            for reverse, flip in attempts:
                sketch_feat, how = self._sketch(shape, mirror)
                exact_transform = how == "transform"
                feature = None
                try:
                    feature = self._extrude(shape, sketch_feat, reverse, flip, merge)
                except Exception as exc:  # noqa: BLE001 - a rejected direction is just a failed attempt
                    log.info("extrude attempt failed: %s", exc)
                boxes = self._face_boxes(feature) if feature is not None else []
                tried.append(f"reverse={reverse} flip={flip} sketch={how} -> "
                             f"{f'feature({len(boxes)} faces)' if feature is not None else 'none'}")
                if feature is not None and not boxes and not shape.cut:
                    self.delete(feature)
                    log.warning("shape made but it has no faces of its own: %s; %s", shape, tried)
                    raise SwError(
                        Code.SW_ERROR,
                        f"SolidWorks made the {shape.label.lower()} but it has no faces of its own: it is either "
                        "completely inside the part, or only touches it along a face or an edge (zero thickness).",
                        "Move or resize it so it clearly overlaps the part (by at least 0.5 mm), or leave a clear gap.",
                    )
                if feature is not None and placement_ok(shape, boxes):
                    log.info("built %s %s..%s along %s; attempts: %s",
                             shape.label, shape.start, shape.end, shape.axis, "; ".join(tried))
                    return feature
                self.delete(feature if feature is not None else sketch_feat)
        log.warning("build failed for %s: %s", shape, tried)
        verb = "cut" if shape.cut else "add"
        raise SwError(
            Code.SW_ERROR,
            f"SolidWorks could not {verb} this shape where it was asked (tried {len(tried)} ways).",
            ("For a cut, make sure the shape overlaps the existing part. For an added shape, make sure it "
             "touches the part. Call get_model_summary to see where the part is."),
        )

    def build(self, shape: Shape) -> dict:
        if shape.tilt and abs(shape.tilt[1]) > 1e-9:
            return self.build_tilted(shape)
        volume_before = self.volume_mm3()
        feature = self._place(shape)
        return self._finish(shape, feature, volume_before)

    def _finish(self, shape: Shape, feature: Any, volume_before: float, label: str | None = None) -> dict:
        _last_group[_doc_key(self.doc)] = [shape] if isinstance(shape, Shape) else []
        name = self.unique_name(label or shape.label)
        try:
            feature.Name = name
        except Exception:  # noqa: BLE001 - keep SolidWorks' own name
            name = try_call(feature, "Name") or name
        self.zoom()
        out: dict[str, Any] = {"feature": name}
        summary = self.summary()
        change = round(summary["volume_mm3"] - volume_before, 1)
        out.update(summary)
        out["volume_change_mm3"] = change
        if shape.cut and change > -0.01:
            out["warning"] = "This cut removed no material. It is probably outside the part; check the coordinates."
        elif not shape.cut and change < 0.01 and summary.get("bodies", 1) <= 1:
            out["warning"] = "This shape added no material. It is probably completely inside the part."
        return out

    # ---------------------------------------------------------------- tilted shapes
    def _body_names(self) -> list[str]:
        return [str(try_call(b, "Name") or "") for b in self.bodies()]

    def _body(self, name: str) -> Any:
        for b in self.bodies():
            if try_call(b, "Name") == name:
                return b
        return None

    def _body_box(self, name: str) -> tuple[list[float], list[float]] | None:
        b = as_list(try_call(self._body(name), "GetBodyBox"))
        return (_m(b[0:3]), _m(b[3:6])) if len(b) >= 6 else None

    def _select_body(self, name: str, append: bool, mark: int) -> bool:
        ext = call(self.doc, "Extension")
        return bool(call(ext, "SelectByID2", name, "SOLIDBODY", 0.0, 0.0, 0.0, append, mark, null_dispatch(), 0))

    def _tool_body_name(self, before: list[str]) -> str | None:
        """The one body that is not part of the original part. SolidWorks renames a body after
        each feature that changes it (e.g. Move/Copy), so it cannot be tracked by name."""
        new = [n for n in self._body_names() if n not in before]
        return new[0] if len(new) == 1 else None

    def _move_body(self, before: list[str], attempts: list[tuple[str, tuple]],
                   expected: tuple[list[float], list[float]], what: str) -> tuple[Any, str]:
        """Move the one new body with Move/Copy Body, trying argument sets until its box lands on
        `expected`. Each attempt is (label, the 10 numbers before bCopy)."""
        fm = call(self.doc, "FeatureManager")
        for label, numbers in attempts:
            name = self._tool_body_name(before)
            if name is None:
                raise SwError(Code.SW_ERROR, f"Lost track of the {what} shape's body.", "Try the step again.")
            call(self.doc, "ClearSelection2", True)
            if not self._select_body(name, False, 1):
                raise SwError(Code.SW_ERROR, f"SolidWorks could not select the new body to {what} it.", "Try again.")
            try:
                feature = call(fm, "InsertMoveCopyBody2", *numbers, False, 1)
            except Exception as exc:  # noqa: BLE001 - a refused argument set is just a failed attempt
                log.info("move body (%s) refused: %s", label, exc)
                feature = None
            moved = self._tool_body_name(before)
            box = self._body_box(moved) if moved else None
            log.info("%s %s: feature=%s body %s -> %s, box %s, expected %s", what, label, feature is not None,
                     name, moved, box, expected)
            if feature is not None and box is not None and boxes_match(box, expected):
                return feature, moved  # type: ignore[return-value]
            self.delete(feature)
        raise SwError(Code.SW_ERROR, f"SolidWorks did not {what} the shape as expected.",
                      "Build the shape without it, or tell the user this SolidWorks version is not supported yet.")

    def _rotate_body(self, before: list[str], shape: Shape) -> tuple[Any, str]:
        axis, deg, about = shape.tilt  # type: ignore[misc]
        pivot = (about[0] / 1000, about[1] / 1000, about[2] / 1000)
        attempts: list[tuple[str, tuple]] = []
        for order in ANGLE_SLOTS:
            for sign in (1.0, -1.0):
                angles = [0.0, 0.0, 0.0]
                angles[order[axis]] = math.radians(deg) * sign
                label = f"tilt slot={order[axis]} sign={sign:+g}"
                if all(a[0] != label for a in attempts):
                    attempts.append((label, (0.0, 0.0, 0.0, 0.0, *pivot, *angles)))
        return self._move_body(before, attempts, tilted_box(shape), "tilt")

    def _translate_body(self, before: list[str], offset: list[float],
                        expected: tuple[list[float], list[float]]) -> tuple[Any, str]:
        d = [v / 1000 for v in offset]
        dist = math.sqrt(sum(v * v for v in d))
        unit = [v / dist for v in d]
        attempts = [("shift xyz", (*d, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
                    ("shift direction+distance", (*unit, dist, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))]
        return self._move_body(before, attempts, expected, "move")

    def _combine(self, main: str, tool: str, cut: bool) -> Any:
        fm = call(self.doc, "FeatureManager")
        op = 15902 if cut else 15903  # swBodyOperationType_e: SWBODYCUT / SWBODYADD
        main_body, tool_body = self._body(main), self._body(tool)
        tools = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                                        [getattr(tool_body, "_oleobj_", tool_body)])
        attempts = (
            lambda: call(fm, "InsertCombineFeature", op, main_body, tools),
            lambda: (call(self.doc, "ClearSelection2", True), self._select_body(main, False, 1),
                     self._select_body(tool, True, 2), call(fm, "InsertCombineFeature", op, None, None))[-1],
        )
        for attempt in attempts:
            try:
                feature = attempt()
            except Exception:  # noqa: BLE001 - try the selection-based form
                feature = None
            if feature is not None and tool not in self._body_names():
                return feature
            self.delete(feature)
        raise SwError(Code.SW_ERROR, f"SolidWorks could not {'cut' if cut else 'join'} the tilted shape.",
                      "Make sure the tilted shape overlaps the part.")

    def _main_body(self, names: list[str]) -> str:
        best, best_vol = names[0], -1.0
        for name in names:
            props = as_list(try_call(self._body(name), "GetMassProperties", 1.0))
            vol = float(props[3]) if len(props) > 3 else 0.0
            if vol > best_vol:
                best, best_vol = name, vol
        return best

    def build_tilted(self, shape: Shape) -> dict:
        volume_before = self.volume_mm3()
        before = self._body_names()
        if shape.cut and not before:
            raise _bad("A tilted cut needs a part to cut from.", "Build the base body first.")
        tool = Shape(shape.axis, shape.profile, shape.start, shape.end, False, shape.label, shape.tilt)
        created: list[Any] = []
        try:
            created.append(self._place(tool, merge=False))
            if self._tool_body_name(before) is None:
                raise SwError(Code.SW_ERROR, "SolidWorks did not make the tilted shape as a separate body.",
                              "Build the shape without rotate.")
            move, tool_name = self._rotate_body(before, shape)
            created.append(move)
            if before:
                created.append(self._combine(self._main_body(before), tool_name, shape.cut))
        except BaseException:
            for feature in reversed(created):
                self.delete(feature)
            raise
        return self._finish(shape, created[-1], volume_before)

    # ---------------------------------------------------------------- revolve
    def revolve(self, spec: Revolve) -> dict:
        placed, offset = spec.shifted()
        if any(abs(v) > 1e-9 for v in offset):
            return self._revolve_moved(spec, placed, offset)
        volume_before = self.volume_mm3()
        feature = self._revolve_feature(spec, merge=True)
        return self._finish(spec, feature, volume_before)  # type: ignore[arg-type]

    def _revolve_moved(self, spec: Revolve, placed: Revolve, offset: list[float]) -> dict:
        """Off-plane axis: revolve a separate body on a default plane, move it, then join or cut."""
        volume_before = self.volume_mm3()
        before = self._body_names()
        if spec.cut and not before:
            raise _bad("A revolved cut needs a part to cut from.", "Build the base body first.")
        tool = Revolve(placed.axis, placed.center, placed.profile, placed.angle, False, placed.label)
        created: list[Any] = []
        try:
            created.append(self._revolve_feature(tool, merge=False))
            made = self._tool_body_name(before)
            box = self._body_box(made) if made else None
            if box is None:
                raise SwError(Code.SW_ERROR, "SolidWorks did not make the revolve as a separate body.",
                              "Put the revolve axis on the Front, Top or Right plane (a center coordinate of 0).")
            expected = ([box[0][i] + offset[i] for i in range(3)], [box[1][i] + offset[i] for i in range(3)])
            move, tool_name = self._translate_body(before, offset, expected)
            created.append(move)
            if before:
                created.append(self._combine(self._main_body(before), tool_name, spec.cut))
        except BaseException:
            for feature in reversed(created):
                self.delete(feature)
            raise
        return self._finish(spec, created[-1], volume_before)  # type: ignore[arg-type]

    def _revolve_feature(self, spec: Revolve, merge: bool) -> Any:
        plane_axis, radial = spec.sketch_plane()
        polygon = [spec.world(r, h, radial) for r, h in spec.profile]
        hs = [h for _, h in spec.profile]
        centerline = (spec.world(0, min(hs), radial), spec.world(0, max(hs), radial))
        expected = spec.box()
        fm = call(self.doc, "FeatureManager")
        angle = math.radians(spec.angle) if spec.angle < 360 else 2 * math.pi
        tried: list[str] = []
        exact = False
        for mirror in (False, True):
            if mirror and exact:
                break
            for reverse in (False, True) if spec.angle < 360 else (False,):
                sketch_feat, how = self._draw(plane_axis, mirror, polygon=polygon, centerline=centerline)
                exact = how == "transform"
                self.select_only(sketch_feat)
                try:
                    feature = call(fm, "FeatureRevolve2", True, True, False, spec.cut, reverse, False, 0, 0,
                                   angle, 0.0, False, False, 0.0, 0.0, 0, 0.0, 0.0, merge, True, True)
                except Exception as exc:  # noqa: BLE001
                    log.info("revolve attempt failed: %s", exc)
                    feature = None
                tried.append(f"reverse={reverse} sketch={how} -> {'feature' if feature else 'none'}")
                if feature is not None and placement_ok(expected, self._face_boxes(feature)):
                    log.info("built %s around %s; attempts: %s", spec.label, spec.axis, "; ".join(tried))
                    return feature
                self.delete(feature if feature is not None else sketch_feat)
        raise SwError(Code.SW_ERROR, f"SolidWorks could not revolve this profile (tried {len(tried)} ways).",
                      "Check that the profile does not cross the axis and, for a cut, that it overlaps the part.")

    # ---------------------------------------------------------------- repeats
    def last_group(self) -> list[Shape]:
        """The last shape, or the whole group (original + copies) made by the previous repeat."""
        group = _last_group.get(_doc_key(self.doc))
        if not group:
            raise SwError(Code.NOT_FOUND, "There is no shape to repeat in this part yet.",
                          "Make the first one with make_box, make_cylinder or make_prism, then repeat it.")
        return group

    def repeat(self, base: list[Shape], copies: list[Shape]) -> dict:
        """Build the copies in order. Stops at the first failure and says how far it got.
        Afterwards the whole group (base + copies) becomes what the next repeat copies."""
        volume_before = self.volume_mm3()
        names: list[str] = []
        made: list[Shape] = []
        key = _doc_key(self.doc)
        try:
            for i, shape in enumerate(copies, 1):
                try:
                    names.append(self.build(shape)["feature"])
                    made.append(shape)
                except SwError as err:
                    err.message = f"Copy {i} of {len(copies)} failed: {err.message}"
                    if names:
                        err.message += f" Copies already made: {', '.join(names)}."
                    raise
        finally:
            _last_group[key] = base + made
        out: dict[str, Any] = {"features": names, "group_size": len(base) + len(made), **self.summary()}
        out["volume_change_mm3"] = round(out["volume_mm3"] - volume_before, 1)
        return out

    def repeat_linear(self, copies: int, dx: float, dy: float, dz: float) -> dict:
        if abs(dx) + abs(dy) + abs(dz) < 0.001:
            raise _bad("The step is zero, so all copies would sit on top of each other.",
                       "Give step_x_mm, step_y_mm or step_z_mm.")
        base = self.last_group()
        self._check_count(len(base) * copies)
        new = [translated(s, dx * k, dy * k, dz * k) for k in range(1, copies + 1) for s in base]
        return self.repeat(base, new)

    def repeat_around(self, copies: int, step_deg: float, center: tuple[float, float, float]) -> dict:
        if abs(step_deg) < 0.01 or abs(step_deg) * copies > 360.001:
            raise _bad(f"angle_step_deg {step_deg} with {copies} copies is not valid.",
                       "For N evenly spaced items make 1 and repeat N-1 copies with angle_step_deg = 360/N.")
        base = self.last_group()
        self._check_count(len(base) * copies)
        new = [rotated(s, step_deg * k, center) for k in range(1, copies + 1) for s in base]
        return self.repeat(base, new)

    @staticmethod
    def _check_count(total: int) -> None:
        if total > MAX_COPIES:
            raise _bad(f"That would make {total} new shapes; the limit is {MAX_COPIES} per call.",
                       "Use fewer copies, or repeat in two steps.")

    # ---------------------------------------------------------------- edges
    def edges(self) -> list[tuple[Any, EdgeInfo]]:
        out = []
        for body in self.bodies():
            for edge in as_list(try_call(body, "GetEdges")):
                params = as_list(try_call(edge, "GetCurveParams2"))
                if len(params) < 6:
                    continue
                curve = try_call(edge, "GetCurve")
                start, end = _m(params[0:3]), _m(params[3:6])
                mid = None
                if len(params) >= 8 and curve is not None:
                    ev = as_list(try_call(curve, "Evaluate2", (params[6] + params[7]) / 2.0, 0))
                    if len(ev) >= 3:
                        mid = _m(ev[0:3])
                if mid is None:
                    mid = [(start[i] + end[i]) / 2 for i in range(3)]
                out.append((edge, EdgeInfo(start, end, mid, try_call(curve, "IsLine") is True,
                                           try_call(curve, "IsCircle") is True)))
        return out

    def _select_edge(self, edge: Any, info: EdgeInfo) -> bool:
        """Add one edge to the selection. Selecting the object itself never misses; clicking a
        3D point (the last resort) can miss edges hidden from the current view."""
        for attempt in (
            lambda: call(edge, "Select4", True, null_dispatch()),
            lambda: call(edge, "Select2", True, 0),
            lambda: call(call(self.doc, "Extension"), "SelectByID2", "", "EDGE",
                         *(c / 1000 for c in info.mid), True, 0, null_dispatch(), 0),
        ):
            try:
                if attempt():
                    return True
            except Exception:  # noqa: BLE001 - try the next way
                continue
        return False

    def finish_edges(self, kind: str, size_mm: float, which: str) -> dict:
        box = self.bbox_mm()
        if box is None:
            raise SwError(Code.NOT_FOUND, "The part has no solid body yet.", "Build a shape first.")
        chosen = [(edge, info) for edge, info in self.edges() if edge_matches(info, which, box[0], box[1])]
        if not chosen:
            raise SwError(Code.NOT_FOUND, f"No edges match '{which}'.",
                          "Use another choice, or call get_model_summary to see the part.")
        self.ensure_active()
        self.exit_sketch()
        call(self.doc, "ClearSelection2", True)
        picked = sum(1 for edge, info in chosen if self._select_edge(edge, info))
        counted = try_call(try_call(self.doc, "SelectionManager"), "GetSelectedObjectCount2", -1)
        if isinstance(counted, int) and counted >= 0:
            picked = counted
        if picked == 0:
            raise SwError(Code.SW_ERROR, "SolidWorks could not select those edges.", "Try another choice.")
        if picked < len(chosen):
            log.warning("finish_edges: selected %d of %d '%s' edges", picked, len(chosen), which)
        volume_before = self.volume_mm3()
        fm = call(self.doc, "FeatureManager")
        size = size_mm / 1000
        feature = None
        if kind == "fillet":
            for args in (
                ("FeatureFillet3", 3, size, 0.0, 0.0, 0, 0, 0, None, None, None, None, None, None, None),
                ("FeatureFillet", 3, size, 0, 0, None, None, None),
            ):
                try:
                    feature = call(fm, *args)
                except Exception:  # noqa: BLE001 - try the older API
                    feature = None
                if feature is not None:
                    break
        else:
            feature = try_call(fm, "InsertFeatureChamfer", 0, 1, size, math.pi / 4, 0.0, 0.0, 0.0, 0.0)
        if feature is None:
            call(self.doc, "ClearSelection2", True)
            raise SwError(
                Code.SW_ERROR,
                f"SolidWorks could not make a {size_mm} mm {kind} on {picked} '{which}' edges.",
                "Use a smaller size (it must be smaller than the thinnest wall next to those edges), "
                "or pick fewer edges.",
            )
        name = self.unique_name("Fillet" if kind == "fillet" else "Chamfer")
        try:
            feature.Name = name
        except Exception:  # noqa: BLE001
            name = try_call(feature, "Name") or name
        self.zoom()
        out = {"feature": name, "edges": picked, **self.summary()}
        out["volume_change_mm3"] = round(out["volume_mm3"] - volume_before, 1)
        return out


def _probe(axis: str) -> list[float]:
    """A point on the default plane of `axis` (meters), used to test the transform."""
    p = [0.011, 0.013, 0.017]
    p[AXIS_INDEX[axis]] = 0.0
    return p


def new_part(app: Any) -> dict:
    doc = create_part(app)
    return {"created": try_call(doc, "GetTitle"),
            "next": "Build the base shape first (make_box or make_cylinder), then add or cut features."}


def create_part(app: Any) -> Any:
    template = try_call(app, "GetUserPreferenceStringValue", 8)  # swDefaultTemplatePart
    if not template:
        template = try_call(app, "GetDocumentTemplate", 1, "", 0, 0.0, 0.0)
    if not template:
        raise SwError(Code.SW_ERROR, "No default part template is set in SolidWorks.",
                      "Ask the user to set one in Tools > Options > Default Templates.")
    doc = call(app, "NewDocument", template, 0, 0.0, 0.0)
    if doc is None:
        raise SwError(Code.SW_ERROR, f"SolidWorks could not create a part from {template}.",
                      "Ask the user to check the default part template.")
    _last_group.pop(_doc_key(doc), None)  # a reused title must not inherit an old shape
    return doc
