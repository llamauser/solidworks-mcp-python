"""A fake SolidWorks part that can actually 'build' axis-aligned extrusions.

Geometry is tracked as boxes (enough to test placement checks, volumes and edge picking).
Two knobs make the fake disagree with the server's first guesses, to prove self-correction:
  reverse_convention: Dir=False extrudes toward -normal instead of +normal
  mirror_second_axis: sketch v-axis is the opposite of the server's fallback table
"""

from __future__ import annotations

import math
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


class FakeBody:
    def __init__(self, doc: "FakePart") -> None:
        self.doc = doc

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
        self.doc.selected = []
        return True

    def SelectByID2(self, name, kind, x, y, z, append, mark, callout, option) -> bool:
        if not append:
            self.doc.selected = []
        self.doc.selected.append(("EDGE", (x, y, z)))
        return True


class FakeFeatureManager:
    def __init__(self, doc: "FakePart") -> None:
        self.doc = doc

    def _extrude(self, cut: bool, reverse: bool, depth: float, t0: int, offset: float, flip_offset: bool):
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
        else:
            pts = [conv(*ln[0]) for ln in sketch.lines]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            lo2, hi2 = (min(xs), min(ys)), (max(xs), max(ys))
            area = abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                           for i in range(len(pts)))) / 2
        lo, hi = [0.0] * 3, [0.0] * 3
        lo[ia], lo[ib], hi[ia], hi[ib] = lo2[0], lo2[1], hi2[0], hi2[1]
        lo[n], hi[n] = s, e
        if cut:
            if not doc.solids or not doc.intersects(lo, hi):
                return None  # "the cut does not intersect the model"
        doc.feature_count += 1
        feat = FakeFeature(doc, f"{'Cut' if cut else 'Boss'}-Extrude{doc.feature_count}", "ICE" if not cut else "Cut")
        feat.faces = [FakeBoxFace(lo, hi)]
        feat.box = (lo, hi)
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
        if not cut:
            doc.solids.append(feat)
        doc.selected = []
        return feat

    def FeatureExtrusion3(self, sd, flip, reverse, t1, t2, d1, d2, *rest):
        t0, offset, flip_offset = rest[-3], rest[-2], rest[-1]
        return self._extrude(False, reverse, d1, t0, offset, flip_offset)

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
        self.max_fillet_m = 1.0
        self.fillet_edges: list = []

    # IModelDoc2 / IPartDoc
    def GetTitle(self) -> str:
        return self.title

    def GetType(self) -> int:
        return 1

    def GetPathName(self) -> str:
        return ""

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
        los = [f.box[0] for f in self.solids]
        his = [f.box[1] for f in self.solids]
        return [min(v[i] for v in los) for i in range(3)], [max(v[i] for v in his) for i in range(3)]

    def intersects(self, lo, hi) -> bool:
        for f in self.solids:
            flo, fhi = f.box
            if all(lo[i] < fhi[i] and hi[i] > flo[i] for i in range(3)):
                return True
        return False

    def GetPartBox(self, exact: bool):
        if not self.solids:
            return None
        lo, hi = self.union_box()
        return (*[v / 1000 for v in lo], *[v / 1000 for v in hi])

    def GetBodies2(self, kind: int, visible: bool):
        return (FakeBody(self),) if self.solids else None


def make_modeling_app():
    """A fake SolidWorks application whose NewDocument creates FakePart documents.

    Used by the tests and by `sw-agent bench` (models are scored without real SolidWorks).
    """
    from .fake_sw import FakeApp

    app = FakeApp()
    app.created = []
    app.closed = []

    def new_document(template, paper, width, height):
        doc = FakePart(f"Part{len(app.created) + 1}")
        app.created.append(doc)
        app.ActiveDoc = doc
        return doc

    app.GetUserPreferenceStringValue = lambda index: r"C:\templates\part.prtdot"
    app.NewDocument = new_document
    app.CloseDoc = lambda title: app.closed.append(title)
    return app
