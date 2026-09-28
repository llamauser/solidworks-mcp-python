"""A complete piston engine from a few numbers: inline, V or boxer, 1-8 cylinders.

Free models cannot plan an engine's geometry reliably (every shaft must sit exactly in its
bore), so the whole design is computed here and built with the same verified plan steps as
build_part. Every part is modeled in the engine's own coordinates:

    crankshaft axis = the X axis; a cylinder at bank angle a points along u = (0, cos a, sin a)
    (a is measured from +Y, turning toward +Z). Inline: a = 0. V: +-bank/2. Boxer: +-90.

The motion is known exactly (a slider-crank per cylinder), so it is written next to the assembly
as <assembly>.motion.json and move_mechanism animates it without relying on the mate solver.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.com_utils import try_call
from ..core.errors import Code, SwError

LAYOUTS = ("inline", "v", "boxer")
FIRING_PATTERN = (0, 1, 1, 0, 0, 1, 1, 0)  # 1 = crank pin opposite its cylinder at the start


def _r(v: float) -> float:
    v = round(v, 3)
    return 0.0 if v == 0 else v


def _yz(a: float, b: float, alpha_deg: float, exact: bool = False) -> list[float]:
    """(y, z) of a point `a` along the bank direction and `b` across it (rounded for plans)."""
    t = math.radians(alpha_deg)
    y, z = a * math.cos(t) - b * math.sin(t), a * math.sin(t) + b * math.cos(t)
    return [y, z] if exact else [_r(y), _r(z)]


def _xyz(x: float, dist: float, alpha_deg: float) -> list[float]:
    y, z = _yz(dist, 0.0, alpha_deg)
    return [_r(x), y, z]


def _rect(a0: float, a1: float, half_width: float, alpha_deg: float) -> list[list[float]]:
    """A rectangle in the Y-Z plane, `a0..a1` along the bank direction, +-half_width across."""
    return [_yz(a0, -half_width, alpha_deg), _yz(a1, -half_width, alpha_deg),
            _yz(a1, half_width, alpha_deg), _yz(a0, half_width, alpha_deg)]


@dataclass
class Cylinder:
    index: int     # 1-based
    x: float       # position along the crankshaft
    alpha: float   # bank angle, degrees from +Y toward +Z
    phi: float     # crank pin angle at the start, same convention

    def pin_distance(self, r: float, rod: float, crank_deg: float = 0.0) -> float:
        """Distance from the crank axis to the piston pin (slider-crank)."""
        beta = math.radians(self.phi + crank_deg - self.alpha)
        return r * math.cos(beta) + math.sqrt(rod * rod - (r * math.sin(beta)) ** 2)


@dataclass
class EngineDesign:
    layout: str
    bore: float
    stroke: float
    rod_length: float
    bank_angle: float
    cylinders: list[Cylinder]
    dims: dict[str, float] = field(default_factory=dict)
    parts: dict[str, dict] = field(default_factory=dict)  # part name -> build_part plan

    @property
    def crank_radius(self) -> float:
        return self.stroke / 2

    def motion(self) -> dict:
        return {
            "kind": "slider_crank", "version": 1, "crank": "crankshaft",
            "axis_point": [0.0, 0.0, 0.0], "axis_direction": [1.0, 0.0, 0.0],
            "crank_radius": self.crank_radius, "rod_length": self.rod_length,
            "cylinders": [{"piston": f"piston{c.index}", "rod": f"rod{c.index}", "x": c.x,
                           "alpha": c.alpha, "phi": c.phi} for c in self.cylinders],
        }


def design(layout: str = "v", cylinders: int = 4, bank_angle: float = 90.0, bore: float = 80.0,
           stroke: float = 70.0, rod_length: float = 0.0, heads: bool = True) -> EngineDesign:
    layout = layout.strip().lower().replace("-", "")
    layout = {"vtype": "v", "flat": "boxer", "straight": "inline", "i": "inline"}.get(layout, layout)
    if layout not in LAYOUTS:
        raise SwError(Code.BAD_ARGUMENT, f"Unknown engine layout '{layout}'.", "Use inline, v or boxer.")
    if not 1 <= cylinders <= 8:
        raise SwError(Code.BAD_ARGUMENT, "An engine here has 1 to 8 cylinders.", "Pick a number from 1 to 8.")
    if layout != "inline" and cylinders % 2:
        raise SwError(Code.BAD_ARGUMENT, f"A {layout} engine needs an even number of cylinders (two banks).",
                      "Use 2, 4, 6 or 8 cylinders, or the inline layout.")
    if not 20 <= bore <= 400:
        raise SwError(Code.BAD_ARGUMENT, "The bore must be 20 to 400 mm.", "A car engine has about 70-100 mm.")
    if not 0.4 * bore <= stroke <= 1.6 * bore:
        raise SwError(Code.BAD_ARGUMENT, "The stroke must be 0.4 to 1.6 times the bore.",
                      f"For a {bore:g} mm bore use a stroke of {0.4 * bore:g}-{1.6 * bore:g} mm.")
    if layout == "v" and not 30 <= bank_angle <= 150:
        raise SwError(Code.BAD_ARGUMENT, "The V angle must be 30 to 150 degrees.", "Common angles are 60 and 90.")
    rod = rod_length or round(1.75 * stroke, 1)
    if rod < 1.3 * stroke / 2 + 0.5 * bore:
        raise SwError(Code.BAD_ARGUMENT, f"A {rod:g} mm connecting rod is too short for this stroke.",
                      f"Use at least {math.ceil(1.3 * stroke / 2 + 0.5 * bore)} mm, or leave it at 0 for automatic.")

    B, r = float(bore), stroke / 2
    d = {
        "journal": 0.55 * B, "crank_pin": 0.45 * B, "wrist_pin": 0.25 * B,
        "rod_width": 0.25 * B, "web": 0.12 * B, "gap": 0.5,
    }
    d["big_end"] = d["crank_pin"] + 0.3 * B
    d["small_end"] = d["wrist_pin"] + 0.2 * B
    d["piston_half"] = 0.4 * B
    envelope = r + d["big_end"] / 2 + 3          # the crank's turning radius
    half = envelope + 0.15 * B                   # crankcase half size
    reach = d["rod_width"] / 2 + d["gap"] + d["web"]  # crank throw half width
    pitch = 1.25 * B if layout == "inline" else max(0.65 * B, 2 * reach + 0.1 * B)
    deck = rod + r + d["piston_half"] + 2
    d.update(envelope=envelope, half=half, reach=reach, pitch=pitch, deck=deck)

    if layout == "inline":
        angles = [0.0] * cylinders
    elif layout == "v":
        angles = [bank_angle / 2 if i % 2 == 0 else -bank_angle / 2 for i in range(cylinders)]
    else:
        angles = [90.0 if i % 2 == 0 else -90.0 for i in range(cylinders)]
    cyls = []
    for i in range(cylinders):
        x = (i - (cylinders - 1) / 2) * pitch
        phi = angles[i] + 180.0 * FIRING_PATTERN[i]
        cyls.append(Cylinder(i + 1, _r(x), angles[i], (phi + 180) % 360 - 180))
    eng = EngineDesign(layout, B, float(stroke), float(rod), float(bank_angle), cyls, d)
    x_first, x_last = cyls[0].x, cyls[-1].x
    wall = 0.25 * B
    xa, xb = x_first - pitch / 2 - wall, x_last + pitch / 2 + wall
    d.update(x_front=xa, x_back=xb)

    # ---------------- block: crankcase, pocket open at the bottom, a barrel and bore per cylinder
    steps: list[dict] = [
        {"op": "box", "x": [_r(xa), _r(xb)], "y": [_r(-half), _r(half)], "z": [_r(-half), _r(half)]},
        {"op": "box", "mode": "cut", "x": [_r(xa + wall), _r(xb - wall)], "y": [_r(-half - 1), _r(envelope)],
         "z": [_r(-envelope), _r(envelope)]},
    ]
    for c in cyls:
        t = math.radians(c.alpha)
        start = half / max(abs(math.cos(t)), abs(math.sin(t))) - 0.1 * B
        steps.append({"op": "cylinder", "start": _xyz(c.x, start, c.alpha), "end": _xyz(c.x, deck, c.alpha),
                      "diameter": _r(1.3 * B)})
    for c in cyls:
        steps.append({"op": "cylinder", "mode": "cut", "start": _xyz(c.x, envelope - 3, c.alpha),
                      "end": _xyz(c.x, deck + 1, c.alpha), "diameter": _r(B)})
    steps.append({"op": "cylinder", "mode": "cut", "start": [_r(xa - 1), 0, 0], "end": [_r(xb + 1), 0, 0],
                  "diameter": _r(d["journal"] + 0.5)})
    eng.parts["block"] = {"steps": steps}

    # ---------------- crankshaft: journals, two webs and a pin per cylinder, a flywheel at the back
    steps = []
    web_half = max(d["journal"], d["crank_pin"]) / 2 + 0.08 * B
    ends = [xa - 0.3 * B] + [v for c in cyls for v in (c.x - reach + d["web"] / 2, c.x + reach - d["web"] / 2)] \
        + [xb + 0.3 * B + 0.15 * B]
    for a0, a1 in zip(ends[0::2], ends[1::2]):
        steps.append({"op": "cylinder", "start": [_r(a0), 0, 0], "end": [_r(a1), 0, 0], "diameter": _r(d["journal"])})
    for c in cyls:
        outline = _rect(-(d["journal"] / 2 + 0.05 * B), r + d["crank_pin"] / 2 + 0.08 * B, web_half, c.phi)
        for side in (-1, 1):
            inner = c.x + side * (d["rod_width"] / 2 + d["gap"])
            steps.append({"op": "prism", "axis": "x", "points": outline,
                          "start": _r(inner), "end": _r(inner + side * d["web"])})
        steps.append({"op": "cylinder", "start": [_r(c.x - reach), *_yz(r, 0, c.phi)],
                      "end": [_r(c.x + reach), *_yz(r, 0, c.phi)], "diameter": _r(d["crank_pin"])})
    steps.append({"op": "cylinder", "start": [_r(xb + 0.3 * B), 0, 0], "end": [_r(xb + 0.45 * B), 0, 0],
                  "diameter": _r(2 * half)})
    eng.parts["crankshaft"] = {"steps": steps}

    # ---------------- per cylinder: a connecting rod and a piston
    for c in cyls:
        pin = r * math.cos(math.radians(c.phi - c.alpha))  # +r or -r along the bank direction
        s0 = c.pin_distance(r, rod)
        xr = (_r(c.x - d["rod_width"] / 2), _r(c.x + d["rod_width"] / 2))
        big, small = [*_yz(pin, 0, c.alpha)], [*_yz(s0, 0, c.alpha)]
        eng.parts[f"rod{c.index}"] = {"steps": [
            {"op": "cylinder", "start": [xr[0], *big], "end": [xr[1], *big], "diameter": _r(d["big_end"])},
            {"op": "prism", "axis": "x", "points": _rect(pin, s0, 0.1 * B, c.alpha), "start": xr[0], "end": xr[1]},
            {"op": "cylinder", "start": [xr[0], *small], "end": [xr[1], *small], "diameter": _r(d["small_end"])},
            {"op": "cylinder", "mode": "cut", "start": [xr[0] - 1, *big], "end": [xr[1] + 1, *big],
             "diameter": _r(d["crank_pin"] + 0.5)},
            {"op": "cylinder", "mode": "cut", "start": [xr[0] - 1, *small], "end": [xr[1] + 1, *small],
             "diameter": _r(d["wrist_pin"] + 0.5)},
        ]}
        slot_half = d["rod_width"] / 2 + d["gap"]
        eng.parts[f"piston{c.index}"] = {"steps": [
            {"op": "cylinder", "start": _xyz(c.x, s0 - d["piston_half"], c.alpha),
             "end": _xyz(c.x, s0 + d["piston_half"], c.alpha), "diameter": _r(B - 0.4)},
            {"op": "prism", "mode": "cut", "axis": "x",
             "points": _rect(s0 - d["piston_half"] - 1, s0 + d["small_end"] / 2 + 1, B / 2 + 1, c.alpha),
             "start": _r(c.x - slot_half), "end": _r(c.x + slot_half)},
            {"op": "cylinder", "mode": "cut", "start": [_r(c.x - B / 2 - 1), *small],
             "end": [_r(c.x + B / 2 + 1), *small], "diameter": _r(d["wrist_pin"])},
        ]}

    # ---------------- cylinder heads, one per bank
    if heads:
        banks: dict[float, list[Cylinder]] = {}
        for c in cyls:
            banks.setdefault(c.alpha, []).append(c)
        names = {1: "head"} if len(banks) == 1 else {1: "head_a", 2: "head_b"}
        for n, (alpha, members) in enumerate(banks.items(), 1):
            head = {"op": "box", "x": [_r(members[0].x - 0.65 * B), _r(members[-1].x + 0.65 * B)],
                    "y": [_r(deck), _r(deck + 0.35 * B)], "z": [_r(-0.65 * B), _r(0.65 * B)]}
            if abs(alpha) > 1e-9:
                head["rotate"] = {"axis": "x", "deg": alpha, "about": [0, 0, 0]}
            eng.parts[names[n]] = {"steps": [head]}
    return eng


# ---------------------------------------------------------------- motion (pure math)
def _rot_x(deg: float, about_mm: list[float]) -> list[float]:
    from .mechanism import rotation_about
    return rotation_about([1.0, 0.0, 0.0], deg, about_mm)


def _shift(t_mm: list[float]) -> list[float]:
    return [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, *[v / 1000 for v in t_mm], 1.0, 0, 0, 0]


def poses(spec: dict, crank_deg: float) -> dict[str, list[float]]:
    """World motion (ArrayData, row vectors) of every moving part at `crank_deg` from the start."""
    from .mechanism import compose
    r, rod = spec["crank_radius"], spec["rod_length"]
    out = {spec["crank"]: _rot_x(crank_deg, spec["axis_point"])}
    for c in spec["cylinders"]:
        cyl = Cylinder(0, c["x"], c["alpha"], c["phi"])
        s0, s = cyl.pin_distance(r, rod), cyl.pin_distance(r, rod, crank_deg)
        u = [0.0, *_yz(1.0, 0.0, c["alpha"], True)]
        out[c["piston"]] = _shift([(s - s0) * v for v in u])
        big0 = [c["x"], *_yz(r, 0, c["phi"], True)]
        big = [c["x"], *_yz(r, 0, c["phi"] + crank_deg, True)]
        small0, small = [0.0, *_yz(s0, 0, c["alpha"], True)], [0.0, *_yz(s, 0, c["alpha"], True)]
        a0 = math.atan2(small0[2] - big0[2], small0[1] - big0[1])
        a1 = math.atan2(small[2] - big[2], small[1] - big[1])
        turn = _rot_x(math.degrees(a1 - a0), big0)
        out[c["rod"]] = compose(turn, _shift([big[k] - big0[k] for k in range(3)]))
    return out


def motion_file(assembly_path: str) -> Path:
    p = Path(assembly_path)
    return p.with_name(p.stem + ".motion.json")


def read_motion(assembly_path: str) -> dict | None:
    if not assembly_path:
        return None
    path = motion_file(assembly_path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- building it in SolidWorks
def build(app: Any, project: str, eng: EngineDesign) -> dict:
    from . import plan as p
    from . import project as pr
    from .mechanism import connect_parts

    pr.split_name(f"{project}/block")  # reject a bad project name before building anything
    built: dict[str, Any] = {}
    for name, steps in eng.parts.items():
        try:
            out = p.execute(app, None, p.Plan.model_validate(steps), save_as=f"{project}/{name}")
        except SwError as err:
            done = f" Parts already saved: {', '.join(built)}." if built else ""
            raise SwError(err.code, f"Building the {name} failed: {err.message}{done}",
                          "Tell the user which part failed. Try other numbers (a larger bore, or another "
                          "stroke), or report it to the developer with the logs.") from None
        built[name] = out.get("size_mm")
    assembly = pr.make_assembly(app, project, ",".join(eng.parts), project)
    motion_file(assembly["assembly"]).write_text(json.dumps(eng.motion(), indent=1), encoding="utf-8")
    joints = connect_parts(try_call(app, "ActiveDoc"), fixed_part="block")
    n = len(eng.cylinders)
    label = {"inline": f"inline {n}-cylinder", "v": f"V{n} ({eng.bank_angle:g} degree V)",
             "boxer": f"boxer (flat) {n}-cylinder"}[eng.layout]
    out: dict[str, Any] = {
        "engine": label,
        "bore_mm": eng.bore, "stroke_mm": eng.stroke, "rod_length_mm": eng.rod_length,
        "displacement_cc": round(math.pi / 4 * eng.bore ** 2 * eng.stroke * len(eng.cylinders) / 1000, 1),
        "parts": built, "assembly": assembly["assembly"], "size_mm": assembly.get("size_mm"),
        "joints": len(joints.get("joints", [])), "moving_parts": joints.get("can_move", []),
    }
    for key in ("warnings", "failed_joints", "warning"):
        if assembly.get(key) or joints.get(key):
            out.setdefault("warnings", []).append(assembly.get(key) or joints.get(key))
    out["next"] = ("Tell the user the engine is ready. To see it run: move_mechanism(part=\"crankshaft\") "
                   "turns it (each piston travels one stroke), or make_motion_study for a SolidWorks study.")
    return out
