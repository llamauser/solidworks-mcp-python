"""An in-memory stand-in for the SolidWorks COM object tree.

Methods are real Python methods (pywin32 "with type info" style) and properties are plain
attributes, so the production helpers (`call`, `try_call`) are exercised exactly as with
the real application. Units are SI, as in the SolidWorks API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _set_out(outs: tuple, *values: int) -> None:
    for var, val in zip(outs, values):
        var.value = val


# ---------------------------------------------------------------- dimensions / features
class FakeDimension:
    def __init__(self, name: str, owner: str, value: float, kind: int = 1, doc_title: str = "Block.SLDPRT",
                 read_only: bool = False, max_ok: float | None = None) -> None:
        self.Name = name
        self.FullName = f"{name}@{owner}@{doc_title.rsplit('.', 1)[0]}.Part"
        self._value = value
        self._kind = kind
        self.ReadOnly = read_only
        self.max_ok = max_ok  # values above this make the "rebuild" fail

    @property
    def SystemValue(self) -> float:
        return self._value

    @SystemValue.setter
    def SystemValue(self, v: float) -> None:
        self._value = v

    def GetType(self) -> int:
        return self._kind


class FakeDisplayDimension:
    def __init__(self, dim: FakeDimension) -> None:
        self._dim = dim

    def GetDimension2(self, index: int) -> FakeDimension:
        return self._dim


class FakeFeature:
    def __init__(self, name: str, type_name: str, dims: list[FakeDimension] | None = None,
                 subs: list[FakeFeature] | None = None, specific: Any = None) -> None:
        self.Name = name
        self._type = type_name
        self._disp = [FakeDisplayDimension(d) for d in (dims or [])]
        self._subs = subs or []
        self._next_sub: FakeFeature | None = None
        self._specific = specific
        for a, b in zip(self._subs, self._subs[1:]):
            a._next_sub = b

    def GetTypeName2(self) -> str:
        return self._type

    def GetFirstDisplayDimension(self):
        return self._disp[0] if self._disp else None

    def GetNextDisplayDimension(self, current):
        i = self._disp.index(current)
        return self._disp[i + 1] if i + 1 < len(self._disp) else None

    def GetFirstSubFeature(self):
        return self._subs[0] if self._subs else None

    def GetNextSubFeature(self):
        return self._next_sub

    def GetSpecificFeature2(self):
        if self._specific is None:
            raise AttributeError("<unknown>.GetSpecificFeature2")
        return self._specific


# ---------------------------------------------------------------- geometry
class FakeSurface:
    def __init__(self, kind: str, params: tuple) -> None:
        self._kind = kind
        if kind == "plane":
            self.PlaneParams = params
        elif kind == "cylinder":
            self.CylinderParams = params

    def IsPlane(self) -> bool:
        return self._kind == "plane"

    def IsCylinder(self) -> bool:
        return self._kind == "cylinder"

    def IsCone(self) -> bool:
        return False

    def IsSphere(self) -> bool:
        return False

    def IsTorus(self) -> bool:
        return False


class FakeFace:
    def __init__(self, surface: FakeSurface, area: float, feature: FakeFeature, normal=(0.0, 0.0, 0.0)) -> None:
        self._surface = surface
        self._area = area
        self._feature = feature
        self.Normal = normal

    def GetSurface(self):
        return self._surface

    def GetArea(self) -> float:
        return self._area

    def GetFeature(self):
        return self._feature


class FakeCurve:
    def __init__(self, kind: str, circle_params: tuple | None = None, length: float = 0.0) -> None:
        self._kind = kind
        self._length = length
        if circle_params:
            self.CircleParams = circle_params

    def IsLine(self) -> bool:
        return self._kind == "line"

    def IsCircle(self) -> bool:
        return self._kind == "circle"

    def GetLength3(self, u0: float, u1: float) -> float:
        return self._length


class FakeEdge:
    def __init__(self, curve: FakeCurve, start, end, faces=()) -> None:
        self._curve = curve
        self._params = (*start, *end, 0.0, 1.0, 1.0)
        self._faces = faces

    def GetCurve(self):
        return self._curve

    def GetCurveParams2(self):
        return self._params

    def GetTwoAdjacentFaces2(self):
        return tuple(self._faces)


class FakeComponent:
    def __init__(self, name: str) -> None:
        self.Name2 = name

    def GetPathName(self) -> str:
        return f"C:\\Parts\\{self.Name2.rsplit('-', 1)[0]}.SLDPRT"

    def IsFixed(self) -> bool:
        return False


class FakeMateEntity:
    def __init__(self, comp: FakeComponent) -> None:
        self.ReferenceComponent = comp


class FakeMate:
    def __init__(self, kind: int, comps: list[FakeComponent]) -> None:
        self.Type = kind
        self._ents = [FakeMateEntity(c) for c in comps]

    def GetMateEntityCount(self) -> int:
        return len(self._ents)

    def MateEntity(self, i: int):
        return self._ents[i]


# ---------------------------------------------------------------- selection / document
class FakeSelectionMgr:
    def __init__(self) -> None:
        self.items: list[tuple[int, Any, Any]] = []  # (sel_type, obj, component)

    def GetSelectedObjectCount2(self, mark: int) -> int:
        return len(self.items)

    def GetSelectedObjectType3(self, i: int, mark: int) -> int:
        return self.items[i - 1][0]

    def GetSelectedObject6(self, i: int, mark: int):
        return self.items[i - 1][1]

    def GetSelectedObjectsComponent4(self, i: int, mark: int):
        return self.items[i - 1][2]


class FakeExtension:
    def __init__(self, doc: FakeDoc) -> None:
        self._doc = doc

    def GetWhatsWrongCount(self) -> int:
        return self._doc.problems

    def SaveAs(self, path, version, options, export_data, errors, warnings) -> bool:
        if self._doc.save_error:
            _set_out((errors, warnings), self._doc.save_error, 0)
            return False
        with open(path, "wb") as fh:
            fh.write(b"fake")
        if os.path.splitext(path)[1].lower() in (".sldprt", ".sldasm", ".slddrw"):
            self._doc.path = path
        _set_out((errors, warnings), 0, 0)
        return True


class FakeSketchManager:
    ActiveSketch = None


@dataclass
class FakeDoc:
    title: str = "Block.SLDPRT"
    doc_type: int = 1
    path: str = ""
    dirty: bool = False
    dims: dict[str, FakeDimension] = field(default_factory=dict)
    problems: int = 0
    save_error: int = 0

    def __post_init__(self) -> None:
        self.SelectionManager = FakeSelectionMgr()
        self.SketchManager = FakeSketchManager()
        self.Extension = FakeExtension(self)
        self.rebuilds = 0

    def GetTitle(self) -> str:
        return self.title

    def GetType(self) -> int:
        return self.doc_type

    def GetPathName(self) -> str:
        return self.path

    def GetSaveFlag(self) -> bool:
        return self.dirty

    def Parameter(self, name: str):
        return self.dims.get(name)

    def EditRebuild3(self) -> bool:
        self.rebuilds += 1
        for d in self.dims.values():
            if d.max_ok is not None and d._value > d.max_ok:
                return False
        return True

    def Save3(self, options, errors, warnings) -> bool:
        _set_out((errors, warnings), self.save_error, 0)
        if not self.save_error:
            self.dirty = False
        return not self.save_error


class FakeOpenSpec:
    def __init__(self, path: str) -> None:
        self.FileName = path
        self.DocumentType = 0
        self.Silent = False
        self.Error = 0
        self.Warning = 0


class FakeApp:
    def __init__(self, revision: str = "33.1.0") -> None:
        self._revision = revision
        self.ActiveDoc: FakeDoc | None = None
        self.Visible = False
        self.docs: dict[str, FakeDoc] = {}
        self.alive = True
        self.open_error = 0

    def RevisionNumber(self) -> str:
        if not self.alive:
            import pywintypes
            raise pywintypes.com_error(0x80010108 - 2**32, "disconnected", None, None)
        return self._revision

    def GetDocumentCount(self) -> int:
        return len(self.docs)

    def GetOpenDocumentByName(self, path: str):
        return self.docs.get(path.lower())

    def GetOpenDocSpec(self, path: str) -> FakeOpenSpec:
        return FakeOpenSpec(path)

    def OpenDoc7(self, spec: FakeOpenSpec):
        if self.open_error:
            spec.Error = self.open_error
            return None
        doc = FakeDoc(title=os.path.basename(spec.FileName), doc_type=spec.DocumentType, path=spec.FileName)
        self.docs[spec.FileName.lower()] = doc
        self.ActiveDoc = doc
        return doc

    def ActivateDoc3(self, title, use_prefs, option, errors):
        for doc in self.docs.values():
            if doc.title == title:
                self.ActiveDoc = doc
        return self.ActiveDoc


def block_part() -> tuple[FakeDoc, dict[str, Any]]:
    """A 40 x 20 x 10 mm block: Sketch1 (two widths) extruded by Boss-Extrude1 (depth)."""
    doc = FakeDoc(title="Block.SLDPRT", path="C:\\Parts\\Block.SLDPRT")
    width = FakeDimension("D1", "Sketch1", 0.040)
    height = FakeDimension("D2", "Sketch1", 0.020)
    depth = FakeDimension("D1", "Boss-Extrude1", 0.010, max_ok=0.5)
    sketch = FakeFeature("Sketch1", "ProfileFeature", [width, height])
    boss = FakeFeature("Boss-Extrude1", "Extrusion", [depth], subs=[sketch])
    doc.dims = {"D1@Sketch1": width, "D2@Sketch1": height, "D1@Boss-Extrude1": depth}
    top = FakeFace(FakeSurface("plane", (0, 1, 0, 0, 0.01, 0, 0)), 0.0008, boss, normal=(0.0, 1.0, -0.0))
    hole = FakeFace(FakeSurface("cylinder", (0.005, 0, 0, 0, 1, 0, 0.0025)), 0.00016, boss)
    edge = FakeEdge(FakeCurve("line", length=0.04), (-0.02, 0.01, 0.01), (0.02, 0.01, 0.01), faces=(top,))
    return doc, {"top": top, "hole": hole, "edge": edge, "depth": depth, "boss": boss}
