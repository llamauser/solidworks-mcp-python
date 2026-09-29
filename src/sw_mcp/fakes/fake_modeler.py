"""A fake SolidWorks part that can actually 'build' axis-aligned extrusions.

Geometry is tracked as boxes (enough to test placement checks, volumes and edge picking).
Two knobs make the fake disagree with the server's first guesses, to prove self-correction:
  reverse_convention: Dir=False extrudes toward -normal instead of +normal
  mirror_second_axis: sketch v-axis is the opposite of the server's fallback table
"""

from __future__ import annotations

import math
import os
from typing import Any

PLANE_AXIS = {"Front": 2, "Top": 1, "Right": 0}
IN_PLANE = {0: (1, 2), 1: (0, 2), 2: (0, 1)}


class FakeFeature:
    def __init__(self, doc: "FakePart", name: str, type_name: str) -> None:
        self.doc = doc
        self.Name = name
        self._type = type_name
        self.faces: list = []

    def GetTypeName2(self) -> str:
        return self._type

    def Select2(self, append: bool, mark: int) -> bool:
        if not append:
            self.doc.selected = []
        self.doc.selected.append(self)
        return True

    def GetFaces(self):
        return tuple(self.faces)

    def GetNextFeature(self):
        feats = self.doc.features
        i = feats.index(self)
        return feats[i + 1] if i + 1 < len(feats) else None


class FakeBoxFace:
    def __init__(self, lo, hi) -> None:
        self._box = (*[v / 1000 for v in lo], *[v / 1000 for v in hi])

    def GetBox(self):
        return self._box


class FakeSketch:
    def __init__(self, plane: str) -> None:
        self.plane = plane
        self.circles: list = []
        self.lines: list = []
        self.centerline = None

    # no ModelToSketchTransform: the server must use its fallback table


class FakeSketchManager:
    def __init__(self, doc: "FakePart") -> None:
        self.doc = doc
        self.ActiveSketch: FakeSketch | None = None
        self.AddToDB = False

    def InsertSketch(self, update: bool) -> None:
        if self.ActiveSketch is None:
            sel = self.doc.selected
            if not sel or sel[0].GetTypeName2() != "RefPlane":
                raise AssertionError("InsertSketch without a selected plane")
            self.ActiveSketch = FakeSketch(sel[0].plane)
        else:
            sk = self.ActiveSketch
            self.ActiveSketch = None
            self.doc.sketch_count += 1
            feat = FakeFeature(self.doc, f"Sketch{self.doc.sketch_count}", "ProfileFeature")
            feat.sketch = sk
            self.doc.features.append(feat)

    def CreateLine(self, x1, y1, z1, x2, y2, z2):
        self.ActiveSketch.lines.append(((x1, y1), (x2, y2)))
        return object()

    def CreateCircleByRadius(self, x, y, z, r):
        self.ActiveSketch.circles.append(((x, y), r))
        return object()

    def CreateCenterLine(self, x1, y1, z1, x2, y2, z2):
        self.ActiveSketch.centerline = ((x1, y1), (x2, y2))
        return object()


class FakeMass:
    def __init__(self, v: float) -> None:
        self.Volume = v


class FakeCurve:
    def __init__(self) -> None:
        pass

    def IsLine(self) -> bool:
        return True

    def IsCircle(self) -> bool:
        return False


class FakeEdge:
    def __init__(self, a, b, doc=None) -> None:
        self._p = (*[v / 1000 for v in a], *[v / 1000 for v in b], 0.0, 1.0, 1.0)
        self._doc = doc

    def Select4(self, append, data) -> bool:
        if self._doc is None:
            raise AttributeError("<unknown>.Select4")
        if not append:
            self._doc.selected = []
        mid = tuple((self._p[i] + self._p[i + 3]) / 2 for i in range(3))
        self._doc.selected.append(("EDGE", mid))
        return True

    def GetCurveParams2(self):
        return self._p

    def GetCurve(self):
        return FakeCurve()


def _rotate(p, angles, pivot):
    x, y, z = (p[i] - pivot[i] for i in range(3))
    ax, ay, az = angles
    y, z = y * math.cos(ax) - z * math.sin(ax), y * math.sin(ax) + z * math.cos(ax)
    x, z = x * math.cos(ay) + z * math.sin(ay), -x * math.sin(ay) + z * math.cos(ay)
    x, y = x * math.cos(az) - y * math.sin(az), x * math.sin(az) + y * math.cos(az)
    return [x + pivot[0], y + pivot[1], z + pivot[2]]


def _bbox(points):
    return [min(p[i] for p in points) for i in range(3)], [max(p[i] for p in points) for i in range(3)]


class FakeSolidBody:
    """A separate body (created with Merge=False): tracked by its outline points (mm)."""

    def __init__(self, doc: "FakePart", name: str, points: list, volume_m3: float) -> None:
        self.doc = doc
        self.Name = name
        self.points = points
        self.volume_m3 = volume_m3

    def box(self):
        return _bbox(self.points)

    def GetBodyBox(self):
        lo, hi = self.box()
        return (*[v / 1000 for v in lo], *[v / 1000 for v in hi])

    def GetMassProperties(self, density):
        return (0.0, 0.0, 0.0, self.volume_m3, 0.0, 0.0)

    def GetEdges(self):
        return ()


class FakeBody:
    """The part's merged solid. With `group` (feature boxes that touch), one of several pieces."""

    def __init__(self, doc: "FakePart", group: list | None = None, index: int = 1) -> None:
        self.doc = doc
        self.group = group
        self.Name = f"Body{index}"

    def _box(self):
        if self.group is None:
            return self.doc.union_box()
        return _bbox([corner for f in self.group for corner in f.box])

    def GetBodyBox(self):
        lo, hi = self._box()
        return (*[v / 1000 for v in lo], *[v / 1000 for v in hi])

    def GetMassProperties(self, density):
        extra = sum(b.volume_m3 for b in self.doc.extra_bodies)
        total = self.doc.volume_m3 - extra
        if self.group is not None and len(self.doc.pieces()) > 1:  # split by box volume
            lo, hi = self._box()
            total = (hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2]) / 1e9
        return (0.0, 0.0, 0.0, total, 0.0, 0.0)

    def GetEdges(self):
        lo, hi = self.doc.union_box()
        edges = []
        xs, ys, zs = (lo[0], hi[0]), (lo[1], hi[1]), (lo[2], hi[2])
        for y in ys:
            for z in zs:
                edges.append(FakeEdge((xs[0], y, z), (xs[1], y, z), self.doc))
        for x in xs:
            for z in zs:
                edges.append(FakeEdge((x, ys[0], z), (x, ys[1], z), self.doc))
        for x in xs:
            for y in ys:
                edges.append(FakeEdge((x, y, zs[0]), (x, y, zs[1]), self.doc))
        return tuple(edges)


class FakeExtension:
    def __init__(self, doc: "FakePart") -> None:
        self.doc = doc

    def CreateMassProperty(self):
        return FakeMass(self.doc.volume_m3)

    def DeleteSelection2(self, options: int) -> bool:
        for feat in list(self.doc.selected):
            if feat in self.doc.features:
                self.doc.features.remove(feat)
                self.doc.volume_m3 -= getattr(feat, "volume_m3", 0.0)
                if feat in self.doc.solids:
                    self.doc.solids.remove(feat)
                self.doc.cylinders = [c for c in self.doc.cylinders if c["feature"] is not feat]
                body = getattr(feat, "body", None)
                if body is not None and body in self.doc.extra_bodies:
                    self.doc.extra_bodies.remove(body)
                undo = getattr(feat, "undo", None)
                if undo is not None:
                    undo()
        self.doc.selected = []
        return True

    def SelectByID2(self, name, kind, x, y, z, append, mark, callout, option) -> bool:
        if not append:
            self.doc.selected = []
        if kind == "SOLIDBODY":
            for body in self.doc.extra_bodies:
                if body.Name == name:
                    self.doc.selected.append(body)
                    return True
            if name == "Body1" and self.doc.solids:
                self.doc.selected.append(FakeBody(self.doc))
                return True
            return False
        self.doc.selected.append(("EDGE", (x, y, z)))
        return True

    def SaveAs(self, path, version, options, export_data, errors, warnings) -> bool:
        with open(path, "wb") as fh:
            fh.write(b"fake")
        if path.lower().endswith(".sldprt"):  # like SOLIDWORKS: the window takes the file's name
            self.doc.path = path
            self.doc.title = os.path.basename(path)
        errors.value = 0
        warnings.value = 0
        return True


class FakeFeatureManager:
    def __init__(self, doc: "FakePart") -> None:
        self.doc = doc

    def _extrude(self, cut: bool, reverse: bool, depth: float, t0: int, offset: float, flip_offset: bool,
                 merge: bool = True):
        doc = self.doc
        sk_feat = doc.selected[0]
        sketch: FakeSketch = sk_feat.sketch
        n = PLANE_AXIS[sketch.plane]
        ia, ib = IN_PLANE[n]
        sign = -1.0 if doc.reverse_convention else 1.0
        direction = -sign if reverse else sign
        if cut:
            direction = -direction  # like SOLIDWORKS: a cut needs reverse=True to go along +normal
        start = 0.0 if t0 == 0 else (-offset if flip_offset else offset)
        s, e = sorted((start * 1000, (start + direction * depth) * 1000))

        def conv(u: float, v: float) -> tuple[float, float]:
            """Sketch (u, v) in meters -> in-plane world (a, b) in mm: the inverse of the
            server's fallback table, optionally with the v-axis mirrored."""
            if doc.mirror_second_axis:
                v = -v
            if n == 2:  # Front: (x, y) = (u, v)
                return u * 1000, v * 1000
            if n == 1:  # Top: (x, z) = (u, -v)
                return u * 1000, -v * 1000
            return v * 1000, -u * 1000  # Right: (y, z) = (v, -u)
        if sketch.circles:
            (u, v), r = sketch.circles[0]
            a, b = conv(u, v)
            lo2, hi2 = (a - r * 1000, b - r * 1000), (a + r * 1000, b + r * 1000)
            area = math.pi * (r * 1000) ** 2
            ring = [(a + r * 1000 * math.cos(k * math.pi / 36), b + r * 1000 * math.sin(k * math.pi / 36))
                    for k in range(72)]
        else:
            pts = [conv(*ln[0]) for ln in sketch.lines]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            lo2, hi2 = (min(xs), min(ys)), (max(xs), max(ys))
            area = abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                           for i in range(len(pts)))) / 2
            ring = pts
        lo, hi = [0.0] * 3, [0.0] * 3
        lo[ia], lo[ib], hi[ia], hi[ib] = lo2[0], lo2[1], hi2[0], hi2[1]
        lo[n], hi[n] = s, e
        if cut:
            if not doc.intersects(lo, hi):
                return None  # "the cut does not intersect the model"
        doc.feature_count += 1
        feat = FakeFeature(doc, f"{'Cut' if cut else 'Boss'}-Extrude{doc.feature_count}", "ICE" if not cut else "Cut")
        feat.faces = [] if (doc.broken_bosses and not cut) else [FakeBoxFace(lo, hi)]
        if feat.faces and not cut:
            # Like SOLIDWORKS: an end cap flush with the side of an older feature merges with that
            # face, so the cap's face reaches over the older feature too.
            for along in (s, e):
                clo, chi = list(lo), list(hi)
                clo[n] = chi[n] = along
                for old in doc.solids:
                    olo, ohi = old.box
                    if (abs(olo[n] - along) < 1e-6 or abs(ohi[n] - along) < 1e-6) and all(
                            clo[i] < ohi[i] and chi[i] > olo[i] for i in (ia, ib)):
                        clo = [min(clo[i], olo[i]) if i != n else along for i in range(3)]
                        chi = [max(chi[i], ohi[i]) if i != n else along for i in range(3)]
                feat.faces.append(FakeBoxFace(clo, chi))
        feat.box = (lo, hi)
        if sketch.circles:
            center = [0.0] * 3
            center[ia], center[ib] = a, b
            axis_vec = [0.0] * 3
            axis_vec[n] = 1.0
            p0, p1 = list(center), list(center)
            p0[n], p1[n] = s, e
            doc.cylinders.append({"p0": p0, "p1": p1, "radius": r * 1000, "feature": feat})
        length = e - s
        if cut:
            # Like SolidWorks, a cut only removes material that is there: clip its length along
            # the extrusion axis to the solid's extent (enough for through holes and pockets).
            ulo, uhi = doc.union_box()
            length = max(0.0, min(e, uhi[n]) - max(s, ulo[n]))
        vol = area * length / 1e9
        feat.volume_m3 = -vol if cut else vol
        doc.volume_m3 += feat.volume_m3
        doc.features[doc.features.index(sk_feat)] = feat  # the sketch is absorbed
        if not cut and not merge:
            points = []
            for along in (s, e):
                for a, b in ring:
                    pt = [0.0] * 3
                    pt[ia], pt[ib], pt[n] = a, b, along
                    points.append(pt)
            doc.body_count += 1
            feat.body = FakeSolidBody(doc, f"{feat.Name}-Body", points, vol)
            doc.extra_bodies.append(feat.body)
        elif not cut:
            doc.solids.append(feat)
        doc.selected = []
        return feat

    def FeatureExtrusion3(self, sd, flip, reverse, t1, t2, d1, d2, *rest):
        t0, offset, flip_offset, merge = rest[-3], rest[-2], rest[-1], rest[-6]
        return self._extrude(False, reverse, d1, t0, offset, flip_offset, merge)

    def InsertMoveCopyBody2(self, tx, ty, tz, td, px, py, pz, ax, ay, az, copy, count):
        doc = self.doc
        bodies = [b for b in doc.selected if isinstance(b, FakeSolidBody)]
        if not bodies:
            return None
        body = bodies[0]
        old = body.points
        k = doc.rotation_sign
        pivot = (px * 1000, py * 1000, pz * 1000)
        # Like SOLIDWORKS 2024 (measured): the three angle slots turn about Z, Y and X.
        about_x, about_y, about_z = (ax, ay, az) if doc.documented_angle_order else (az, ay, ax)
        shift = [tx * 1000, ty * 1000, tz * 1000] if not td else [0.0, 0.0, 0.0]
        def move(p):
            return [c + s for c, s in zip(_rotate(p, (about_x * k, about_y * k, about_z * k), pivot), shift)]

        body.points = [move(p) for p in old]
        cyls = [c for c in doc.cylinders if getattr(c["feature"], "body", None) is body]
        old_ends = [(c["p0"], c["p1"]) for c in cyls]
        for c in cyls:
            c["p0"], c["p1"] = move(c["p0"]), move(c["p1"])
        doc.feature_count += 1
        feat = FakeFeature(doc, f"Body-Move/Copy{doc.feature_count}", "MoveCopyBody")
        old_name = body.Name
        body.Name = feat.Name

        def undo() -> None:
            body.points = old
            body.Name = old_name
            for c, (p0, p1) in zip(cyls, old_ends):
                c["p0"], c["p1"] = p0, p1

        feat.undo = undo
        doc.features.append(feat)
        doc.selected = []
        return feat

    def InsertCombineFeature(self, op, main, tools):
        doc = self.doc
        tool_list = getattr(tools, "value", tools) or []
        if not tool_list or main is None:
            return None
        tool = tool_list[0]
        if tool not in doc.extra_bodies:
            return None
        lo, hi = tool.box()
        doc.extra_bodies.remove(tool)
        doc.feature_count += 1
        feat = FakeFeature(doc, f"Combine{doc.feature_count}", "CombineBodies")
        if op == 15903:  # add
            feat.box = (lo, hi)
            doc.solids.append(feat)
        else:  # cut: the tool body disappears and takes the overlapping material with it
            ulo, uhi = doc.union_box()
            overlap = 1.0
            for i in range(3):
                span = hi[i] - lo[i]
                inside = max(0.0, min(hi[i], uhi[i]) - max(lo[i], ulo[i]))
                overlap *= inside / span if span > 0 else 1.0
            feat.volume_m3 = -tool.volume_m3 * overlap
            doc.volume_m3 += feat.volume_m3 - tool.volume_m3
        feat.faces = [FakeBoxFace(lo, hi)]
        doc.features.append(feat)
        return feat

    def FeatureRevolve2(self, single, solid, thin, is_cut, reverse, both, t1, t2, angle, *rest):
        doc = self.doc
        sk_feat = doc.selected[0]
        sketch: FakeSketch = sk_feat.sketch
        if sketch.centerline is None or not sketch.lines:
            return None
        n = PLANE_AXIS[sketch.plane]
        ia, ib = IN_PLANE[n]

        def world(u, v):
            if doc.mirror_second_axis:
                v = -v
            if n == 2:
                a, b = u * 1000, v * 1000
            elif n == 1:
                a, b = u * 1000, -v * 1000
            else:
                a, b = v * 1000, -u * 1000
            p = [0.0] * 3
            p[ia], p[ib] = a, b
            return p

        c1, c2 = world(*sketch.centerline[0]), world(*sketch.centerline[1])
        ax = next(i for i in range(3) if abs(c1[i] - c2[i]) > 1e-9)
        pts = [world(*ln[0]) for ln in sketch.lines]
        radial = [i for i in range(3) if i not in (ax, n)][0]
        rh = [(abs(p[radial] - c1[radial]), p[ax]) for p in pts]
        area2 = sum(rh[i][0] * rh[(i + 1) % len(rh)][1] - rh[(i + 1) % len(rh)][0] * rh[i][1] for i in range(len(rh)))
        area = abs(area2) / 2
        cx = sum((rh[i][0] + rh[(i + 1) % len(rh)][0]) *
                 (rh[i][0] * rh[(i + 1) % len(rh)][1] - rh[(i + 1) % len(rh)][0] * rh[i][1])
                 for i in range(len(rh))) / (3 * area2) if area2 else 0.0
        vol = angle * area * abs(cx) / 1e9
        rmax = max(r for r, _ in rh)
        lo, hi = [0.0] * 3, [0.0] * 3
        for i in range(3):
            if i == ax:
                lo[i], hi[i] = min(h for _, h in rh), max(h for _, h in rh)
            else:
                lo[i], hi[i] = c1[i] - rmax, c1[i] + rmax
        if is_cut and not doc.intersects(lo, hi):
            return None
        merge = rest[-3] if len(rest) >= 3 else True
        doc.feature_count += 1
        feat = FakeFeature(doc, f"Revolve{doc.feature_count}", "Revolution" if not is_cut else "RevCut")
        feat.faces = [FakeBoxFace(lo, hi)]
        feat.box = (lo, hi)
        feat.volume_m3 = -vol if is_cut else vol
        doc.volume_m3 += feat.volume_m3
        doc.features[doc.features.index(sk_feat)] = feat
        if not is_cut and not merge:
            others = [i for i in range(3) if i != ax]
            points = []
            for _, h in rh:
                for i0 in (0, 1):
                    for sgn in (-1.0, 1.0):
                        p = [0.0] * 3
                        p[ax] = h
                        p[others[1 - i0]] = c1[others[1 - i0]]
                        p[others[i0]] = c1[others[i0]] + sgn * rmax
                        points.append(p)
            feat.body = FakeSolidBody(doc, f"{feat.Name}-Body", points, vol)
            doc.extra_bodies.append(feat.body)
        elif not is_cut:
            doc.solids.append(feat)
        doc.selected = []
        return feat

    def FeatureCut4(self, sd, flip, reverse, t1, t2, d1, d2, *rest):
        t0, offset, flip_offset = rest[-4], rest[-3], rest[-2]
        return self._extrude(True, reverse, d1, t0, offset, flip_offset)

    def FeatureFillet3(self, *args):
        if args[1] > self.doc.max_fillet_m:
            return None
        self.doc.feature_count += 1
        feat = FakeFeature(self.doc, f"Fillet{self.doc.feature_count}", "Fillet")
        self.doc.features.append(feat)
        self.doc.fillet_edges = [s for s in self.doc.selected if isinstance(s, tuple)]
        return feat

    def InsertFeatureChamfer(self, *args):
        self.doc.feature_count += 1
        feat = FakeFeature(self.doc, f"Chamfer{self.doc.feature_count}", "Chamfer")
        self.doc.features.append(feat)
        return feat


class FakePart:
    Visible = True

    def GetSaveFlag(self) -> bool:
        return not self.path  # built parts are unsaved until SaveAs gives them a path

    def __init__(self, title: str = "Part1") -> None:
        self.title = title
        self.features: list[FakeFeature] = [FakeFeature(self, "Origin", "OriginProfileFeature")]
        for name in ("Front", "Top", "Right"):
            plane = FakeFeature(self, f"{name} Plane", "RefPlane")
            plane.plane = name
            self.features.append(plane)
        self.selected: list = []
        self.SketchManager = FakeSketchManager(self)
        self.FeatureManager = FakeFeatureManager(self)
        self.Extension = FakeExtension(self)
        self.volume_m3 = 0.0
        self.solids: list = []
        self.sketch_count = 0
        self.feature_count = 0
        self.reverse_convention = False
        self.mirror_second_axis = False
        self.rotation_sign = 1.0  # -1 makes the fake rotate the other way than the server expects
        self.documented_angle_order = False  # True: Move/Copy angle slots in the documented X, Y, Z order
        self.broken_bosses = False  # True: bosses come out with no faces (zero-thickness contact)
        self.cylinders: list = []  # {"p0", "p1" (axis end points), "radius"} in mm, for assembly joints
        self.extra_bodies: list = []
        self.body_count = 1
        self.path = ""
        self.max_fillet_m = 1.0
        self.fillet_edges: list = []

    # IModelDoc2 / IPartDoc
    def GetTitle(self) -> str:
        return self.title

    def GetType(self) -> int:
        return 1

    def GetPathName(self) -> str:
        return self.path

    def FirstFeature(self):
        return self.features[0]

    def FeatureByPositionReverse(self, i: int):
        return self.features[-1 - i]

    def FeatureByName(self, name: str):
        return next((f for f in self.features if f.Name == name), None)

    def ClearSelection2(self, all_: bool) -> None:
        self.selected = []

    def ViewZoomtofit2(self) -> None:
        pass

    def union_box(self):
        if not self.solids and self.extra_bodies:
            return _bbox([p for b in self.extra_bodies for p in b.points])
        los = [f.box[0] for f in self.solids]
        his = [f.box[1] for f in self.solids]
        return [min(v[i] for v in los) for i in range(3)], [max(v[i] for v in his) for i in range(3)]

    def intersects(self, lo, hi) -> bool:
        boxes = [f.box for f in self.solids] + [b.box() for b in self.extra_bodies]
        return any(all(lo[i] < fhi[i] and hi[i] > flo[i] for i in range(3)) for flo, fhi in boxes)

    def GetPartBox(self, exact: bool):
        if not self.solids:
            return None
        lo, hi = self.union_box()
        return (*[v / 1000 for v in lo], *[v / 1000 for v in hi])

    def pieces(self) -> list[list]:
        """Solid features grouped by touching boxes (a face contact joins, a gap separates)."""
        groups: list[list] = []
        for f in self.solids:
            joined = [g for g in groups if any(_touch(f.box, h.box) for h in g)]
            merged = [f] + [h for g in joined for h in g]
            groups = [g for g in groups if g not in joined] + [merged]
        return groups

    def GetBodies2(self, kind: int, visible: bool):
        groups = self.pieces()
        if len(groups) <= 1:
            main = (FakeBody(self),) if self.solids else ()
        else:
            main = tuple(FakeBody(self, g, i) for i, g in enumerate(groups, 1))
        return (main + tuple(self.extra_bodies)) or None


def _touch(a, b, tol: float = 1e-6) -> bool:
    """Boxes overlap, or share a face area (not only an edge or a corner)."""
    gaps = [max(a[0][i], b[0][i]) - min(a[1][i], b[1][i]) for i in range(3)]  # > 0: apart on that axis
    return all(g <= tol for g in gaps) and sum(1 for g in gaps if g > -tol) <= 1


def _xf_apply(data, p):
    r, t = data[0:9], data[9:12]
    return [p[0] * r[0 + k] + p[1] * r[3 + k] + p[2] * r[6 + k] + t[k] * 1000 for k in range(3)]


class FakeTransform:
    def __init__(self, data) -> None:
        self.ArrayData = tuple(float(v) for v in data)


class FakeMathUtility:
    def CreateTransform(self, data):
        return FakeTransform(getattr(data, "value", data))


class FakeCylSurface:
    def __init__(self, cyl) -> None:
        d = [b - a for a, b in zip(cyl["p0"], cyl["p1"])]
        n = math.sqrt(sum(c * c for c in d)) or 1.0
        self.CylinderParams = (*[v / 1000 for v in cyl["p0"]], *[c / n for c in d], cyl["radius"] / 1000)

    def IsCylinder(self) -> bool:
        return True


class FakeCylFace:
    def __init__(self, comp: "FakeComponent2", cyl) -> None:
        self.comp = comp
        self.cyl = cyl

    def GetSurface(self):
        return FakeCylSurface(self.cyl)

    def GetBox(self):
        r, p0, p1 = self.cyl["radius"], self.cyl["p0"], self.cyl["p1"]
        d = [b - a for a, b in zip(p0, p1)]
        n = math.sqrt(sum(c * c for c in d)) or 1.0
        pad = [r * math.sqrt(max(0.0, 1 - (c / n) ** 2)) for c in d]  # a disc's extent on each axis
        lo = [min(p0[i], p1[i]) - pad[i] for i in range(3)]
        hi = [max(p0[i], p1[i]) + pad[i] for i in range(3)]
        return (*[v / 1000 for v in lo], *[v / 1000 for v in hi])

    def Select4(self, append, data) -> bool:
        asm = self.comp.asm
        if not append:
            asm.selected = []
        asm.selected.append(self)
        return True


class FakeCompBody:
    def __init__(self, comp: "FakeComponent2") -> None:
        self.comp = comp

    def GetFaces(self):
        return tuple(FakeCylFace(self.comp, c) for c in self.comp.part.cylinders)


class FakeComponent2:
    def __init__(self, part: FakePart, offset, asm=None) -> None:
        self.part = part
        self.asm = asm
        self.Name2 = f"{part.title.rsplit('.', 1)[0]}-1"
        self.offset = offset
        self._xf = FakeTransform([1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0])
        self.fixed = False

    @property
    def Transform2(self):
        return self._xf

    @Transform2.setter
    def Transform2(self, value):
        self._xf = value

    def GetBox(self, include_hidden, include_refs):
        lo, hi = self.part.union_box()
        corners = [[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
        pts = [_xf_apply(self._xf.ArrayData, c) for c in corners]
        blo, bhi = _bbox(pts)
        return (*[(blo[i] + self.offset[i]) / 1000 for i in range(3)], *[(bhi[i] + self.offset[i]) / 1000 for i in range(3)])

    def GetBodies3(self, kind, info):
        return (FakeCompBody(self),)

    def Select4(self, append, data, popup) -> bool:
        if not append:
            self.asm.selected = []
        self.asm.selected.append(self)
        return True


class FakeSelectData:
    Mark = 0


class FakeAsmSelectionMgr:
    def CreateSelectData(self):
        return FakeSelectData()


class FakeMotionStudy:
    def __init__(self, accept_motor: bool) -> None:
        self.Name = "Motion Study 2"
        self.StudyType = 0
        self.duration = None
        self.accept_motor = accept_motor
        self.played = False

    def Activate(self):
        return True

    def SetDuration(self, seconds):
        self.duration = seconds
        return True

    def CreateDefinition(self, kind):
        study = self

        class MotorData:  # like ISimulationMotorFeatureData: object properties + ConstantSpeedMotor(rpm)
            MotorType = kind
            DirectionReference = None
            Location = None
            rpm = None

            def ConstantSpeedMotor(self, speed):
                self.rpm = speed

        self.definition = MotorData()
        return self.definition

    def CreateFeature(self, definition):
        ready = definition.DirectionReference is not None and definition.rpm
        return object() if self.accept_motor and ready else None

    def Calculate(self):
        return True

    def Play(self):
        self.played = True
        return True


class FakeMotionStudyManager:
    def __init__(self, accept_motor: bool) -> None:
        self.study = FakeMotionStudy(accept_motor)

    def CreateMotionStudy(self):
        return self.study


class FakeAssemblyExtension:
    def __init__(self, asm: "FakeAssembly") -> None:
        self.asm = asm

    def GetMotionStudyManager(self):
        if self.asm.motion is None:
            return None
        return self.asm.motion

    def SaveAs(self, path, version, options, export_data, errors, warnings) -> bool:
        with open(path, "wb") as fh:
            fh.write(b"fake assembly")
        self.asm.path = path
        self.asm.title = os.path.basename(path)  # like SOLIDWORKS: the window takes the file's name
        errors.value = 0
        warnings.value = 0
        return True


class FakeAssembly:
    Visible = True

    def GetSaveFlag(self) -> bool:
        return not self.path

    def __init__(self, app, title: str) -> None:
        self.app = app
        self.title = title
        self.path = ""
        self.components: list[FakeComponent2] = []
        self.Extension = FakeAssemblyExtension(self)
        self.SelectionManager = FakeAsmSelectionMgr()
        self.selected: list = []
        self.mates: list = []
        self.motion = FakeMotionStudyManager(accept_motor=True)

    def GetTitle(self) -> str:
        return self.title

    def GetType(self) -> int:
        return 2

    def GetPathName(self) -> str:
        return self.path

    def AddComponent5(self, path, config_option, new_config, use_config, existing_config, x, y, z):
        part = self.app.GetOpenDocumentByName(path)
        if part is None:
            return None
        comp = FakeComponent2(part, self.app.component_offset, self)
        self.components.append(comp)
        return comp

    def GetComponents(self, top_level):
        return tuple(self.components)

    def ClearSelection2(self, all_):
        self.selected = []

    def AddMate5(self, kind, align, flip, *rest):
        faces = [s for s in self.selected if isinstance(s, FakeCylFace)]
        err = rest[-1]
        if len(faces) != 2:
            err.value = 1
            return None
        err.value = 0
        self.mates.append((kind, faces[0].comp.Name2, faces[1].comp.Name2))
        return object()

    def FixComponent(self):
        for c in self.selected:
            c.fixed = True

    def UnfixComponent(self):
        for c in self.selected:
            c.fixed = False

    def EditRebuild3(self):
        return True

    def GraphicsRedraw2(self):
        return None

    def GetBox(self, options):
        boxes = [c.GetBox(False, False) for c in self.components]
        if not boxes:
            return None
        return (*[min(b[i] for b in boxes) for i in range(3)], *[max(b[i + 3] for b in boxes) for i in range(3)])

    def ViewZoomtofit2(self) -> None:
        pass


def make_modeling_app():
    """A fake SolidWorks application whose NewDocument creates FakePart documents.

    Used by the tests and by `sw-agent bench` (models are scored without real SolidWorks).
    """
    from .fake_sw import FakeApp

    app = FakeApp()
    app.created = []
    app.closed = []

    app.component_offset = (0.0, 0.0, 0.0)

    def new_document(template, paper, width, height):
        if template.lower().endswith(".asmdot"):
            doc = FakeAssembly(app, f"Assem{len(app.created) + 1}")
        else:
            doc = FakePart(f"Part{len(app.created) + 1}")
        app.created.append(doc)
        app.ActiveDoc = doc
        return doc

    def open_by_name(path):
        for doc in app.created:
            if getattr(doc, "path", "") and doc.path.lower() == str(path).lower():
                return doc
        return None

    app.GetMathUtility = lambda: FakeMathUtility()
    app.GetUserPreferenceStringValue = lambda index: (r"C:\templates\assembly.asmdot" if index == 9
                                                      else r"C:\templates\part.prtdot")
    app.NewDocument = new_document
    app.GetOpenDocumentByName = open_by_name
    app.CloseDoc = lambda title: app.closed.append(title)
    app.GetDocuments = lambda: tuple(d for d in app.created if d.GetTitle() not in app.closed)

    def activate(title, use_prefs, option, errors):
        for d in app.created:
            if d.GetTitle() == title:
                app.ActiveDoc = d
        errors.value = 0
        return app.ActiveDoc

    app.ActivateDoc3 = activate
    return app
