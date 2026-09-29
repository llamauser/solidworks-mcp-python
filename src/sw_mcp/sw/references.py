"""Named frames and axes shared by every part of a machine.

Models are good at describing parts and bad at trigonometry and at keeping one axis identical in
several parts. So a machine declares its references once (plan_machine), and part plans use them:

    axis input through 0,0,0 along y          -> {"op":"cylinder","on_axis":"input","from":0,"to":200,...}
    frame left_bank origin 0,0,0 turn x 45    -> {"op":"box","frame":"left_bank","x":[..],"y":[..],"z":[..]}

A shape in a frame is described upright in the frame's own coordinates; the program moves and
turns it. Every shaft and bore built on the same named axis lies exactly on it, so the joints
between parts are found automatically.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..core.errors import Code, SwError
from . import modeling as m

_LINE = re.compile(r"^\s*(frame|axis)\s+([\w-]+)\s*:?\s*(.*)$", re.I)
_NUM = r"(-?\d+(?:\.\d+)?)"
_POINT = re.compile(rf"(?:origin|through|at)\s*\(?\s*{_NUM}\s*[,\s]\s*{_NUM}\s*[,\s]\s*{_NUM}", re.I)
_TURN = re.compile(rf"(?:turn(?:ed)?|rotate[d]?)\s+(?:about\s+)?([xyz])\s+(?:by\s+)?{_NUM}", re.I)
_ALONG_AXIS = re.compile(r"along\s+([+-]?)([xyz])\b", re.I)
_ALONG_VEC = re.compile(rf"along\s*\(?\s*{_NUM}\s*[,\s]\s*{_NUM}\s*[,\s]\s*{_NUM}", re.I)

EXAMPLES = ('"axis input through 0,0,0 along y" or "frame left_bank origin 0,0,0 turn x 45" '
            '(one reference per line)')


@dataclass
class Frame:
    name: str
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    axis: str | None = None  # turned about a line parallel to this world axis through the origin
    deg: float = 0.0

    def describe(self) -> str:
        turn = f", turned {self.deg:g} deg about {self.axis.upper()}" if self.axis and self.deg else ""
        return f"frame {self.name}: origin {_pt(self.origin)}{turn}"


@dataclass
class Axis:
    name: str
    point: tuple[float, float, float]
    direction: tuple[float, float, float]  # unit vector

    def at(self, t: float) -> tuple[float, float, float]:
        return tuple(self.point[i] + t * self.direction[i] for i in range(3))  # type: ignore[return-value]

    def world_axis(self) -> tuple[str, int] | None:
        """("x"|"y"|"z", +1/-1) when the axis runs along a world axis."""
        for i, c in enumerate(self.direction):
            if abs(abs(c) - 1.0) < 1e-9:
                return "xyz"[i], (1 if c > 0 else -1)
        return None

    def describe(self) -> str:
        along = self.world_axis()
        d = f"{'-' if along and along[1] < 0 else ''}{along[0]}" if along else _pt(self.direction)
        return f"axis {self.name}: through {_pt(self.point)} along {d}"


@dataclass
class References:
    frames: dict[str, Frame] = field(default_factory=dict)
    axes: dict[str, Axis] = field(default_factory=dict)

    def merged(self, other: "References") -> "References":
        return References({**self.frames, **other.frames}, {**self.axes, **other.axes})

    def describe(self) -> list[str]:
        return [f.describe() for f in self.frames.values()] + [a.describe() for a in self.axes.values()]

    def to_dict(self) -> dict:
        return {"frames": {n: {"origin": list(f.origin), "axis": f.axis, "deg": f.deg} for n, f in self.frames.items()},
                "axes": {n: {"point": list(a.point), "direction": list(a.direction)} for n, a in self.axes.items()}}

    @classmethod
    def from_dict(cls, data: dict | None) -> "References":
        data = data or {}
        frames = {n: Frame(n, tuple(f.get("origin", (0, 0, 0))), f.get("axis"), float(f.get("deg", 0)))
                  for n, f in (data.get("frames") or {}).items()}
        axes = {n: Axis(n, tuple(a["point"]), tuple(a["direction"])) for n, a in (data.get("axes") or {}).items()}
        return cls(frames, axes)  # type: ignore[arg-type]

    def frame(self, name: str) -> Frame:
        if name not in self.frames:
            raise SwError(Code.BAD_ARGUMENT, f"There is no frame called '{name}'.",
                          "Known frames: " + (", ".join(self.frames) or "none") + ". Declare it with plan_machine "
                          f"references, e.g. {EXAMPLES}.")
        return self.frames[name]

    def axis(self, name: str) -> Axis:
        if name not in self.axes:
            raise SwError(Code.BAD_ARGUMENT, f"There is no axis called '{name}'.",
                          "Known axes: " + (", ".join(self.axes) or "none") + ". Declare it with plan_machine "
                          f"references, e.g. {EXAMPLES}.")
        return self.axes[name]


def _pt(p) -> str:
    return "(" + ", ".join(f"{round(float(v), 3):g}" for v in p) + ")"


def parse(text: str) -> References:
    refs = References()
    for raw in re.split(r"[\n;]+", text or ""):
        line = raw.strip().lstrip("-*").strip()
        if not line:
            continue
        match = _LINE.match(line)
        if not match:
            raise SwError(Code.BAD_ARGUMENT, f"Cannot read the reference '{line}'.", f"Write it like {EXAMPLES}.")
        kind, name, rest = match.group(1).lower(), match.group(2), match.group(3)
        point = _POINT.search(rest)
        origin = tuple(float(point.group(i)) for i in (1, 2, 3)) if point else (0.0, 0.0, 0.0)
        if kind == "frame":
            turn = _TURN.search(rest)
            refs.frames[name] = Frame(name, origin, turn.group(1).lower() if turn else None,  # type: ignore[arg-type]
                                      float(turn.group(2)) if turn else 0.0)
            continue
        along = _ALONG_AXIS.search(rest)
        if along:
            d = [0.0, 0.0, 0.0]
            d["xyz".index(along.group(2).lower())] = -1.0 if along.group(1) == "-" else 1.0
        else:
            vec = _ALONG_VEC.search(rest)
            if not vec:
                raise SwError(Code.BAD_ARGUMENT, f"The axis '{name}' needs a direction.",
                              'Add "along x", "along y", "along z" or "along 0,0.7071,0.7071".')
            d = [float(vec.group(i)) for i in (1, 2, 3)]
        length = math.sqrt(sum(c * c for c in d))
        if length < 1e-9:
            raise SwError(Code.BAD_ARGUMENT, f"The axis '{name}' has no direction.", 'Use e.g. "along y".')
        refs.axes[name] = Axis(name, origin, tuple(c / length for c in d))  # type: ignore[arg-type]
    return refs


# ---------------------------------------------------------------- applying them to shapes
def in_frame(shape: m.Shape, frame: Frame) -> m.Shape:
    """A shape described in the frame's coordinates -> the same shape in world coordinates."""
    moved = m.translated(shape, *frame.origin)
    if frame.axis and abs(frame.deg) > 1e-9:
        if moved.tilt is not None:
            raise SwError(Code.BAD_ARGUMENT, f"A slanted or rotated shape inside the turned frame '{frame.name}' "
                                             "is not supported.",
                          "Describe it upright in the frame (a straight cylinder, an unrotated box or prism).")
        moved.tilt = (frame.axis, frame.deg, tuple(float(v) for v in frame.origin))
    return moved


def cylinder_on_axis(axis: Axis, start: float, end: float, diameter: float, cut: bool) -> m.Shape:
    p1, p2 = axis.at(start), axis.at(end)
    return m.cylinder_shape(*p1, *p2, diameter, cut)


def revolve_on_axis(axis: Axis, profile: list[tuple[float, float]], angle: float, cut: bool) -> m.Revolve:
    along = axis.world_axis()
    if along is None:
        raise SwError(Code.BAD_ARGUMENT, f"A revolve needs its axis along X, Y or Z; '{axis.name}' is slanted.",
                      "Revolve on a straight axis, or build the round part from cylinders on the slanted axis.")
    letter, sign = along
    i = "xyz".index(letter)
    world = [(r, axis.point[i] + sign * h) for r, h in profile]
    return m.revolve_spec(letter, axis.point, world, angle, cut)
