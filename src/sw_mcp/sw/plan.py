"""Plan-as-data: a whole part described as one JSON document, checked, then built.

Instead of ~30 tool calls (one request each), the model sends one plan. The plan is
validated without touching SolidWorks (typos and impossible shapes are reported instantly
with the step number), then executed step by step with the same self-verifying primitives
as the single-shape tools. On failure the model gets the failing step and a fix, corrects
it, and sends the plan again.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..core.com_utils import try_call
from ..core.errors import Code, SwError
from . import modeling as m
from . import project

Vec3 = tuple[float, float, float]
Range = tuple[float, float]
Mode = Literal["add", "cut"]


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rotate(BaseModel):
    """Tilt a shape after building it: right-hand rotation about a line parallel to `axis`."""
    model_config = ConfigDict(extra="forbid")
    axis: Literal["x", "y", "z"]
    deg: float
    about: Vec3 = (0.0, 0.0, 0.0)


class BoxStep(_Step):
    op: Literal["box"]
    mode: Mode = "add"
    x: Range
    y: Range
    z: Range
    rotate: Rotate | None = None


class CylinderStep(_Step):
    op: Literal["cylinder"]
    mode: Mode = "add"
    start: Vec3
    end: Vec3
    diameter: float = Field(gt=0)
    rotate: Rotate | None = None


def _pairs(value: Any) -> Any:
    if isinstance(value, str):  # also accept "a,b; a,b; a,b"
        return m.parse_points(value)
    return value


class PrismStep(_Step):
    op: Literal["prism"]
    mode: Mode = "add"
    axis: Literal["x", "y", "z"]
    points: list[tuple[float, float]] = Field(min_length=3, max_length=64)
    start: float
    end: float
    rotate: Rotate | None = None

    _points_from_text = field_validator("points", mode="before")(_pairs)


class RevolveStep(_Step):
    """Spin a half-profile of (radius, position along the axis) points around an axis."""
    op: Literal["revolve"]
    mode: Mode = "add"
    axis: Literal["x", "y", "z"]
    center: Vec3 = (0.0, 0.0, 0.0)
    profile: list[tuple[float, float]] = Field(min_length=3, max_length=64)
    angle: float = 360.0

    _profile_from_text = field_validator("profile", mode="before")(_pairs)


class EdgeStep(_Step):
    op: Literal["fillet", "chamfer"]
    size: float = Field(gt=0)
    edges: Literal[m.EDGE_FILTERS]  # type: ignore[valid-type]


class RepeatStep(_Step):
    op: Literal["repeat"]
    copies: int = Field(ge=1, le=m.MAX_COPIES)
    step: Vec3


class RepeatAroundStep(_Step):
    op: Literal["repeat_around"]
    copies: int = Field(ge=1, le=m.MAX_COPIES)
    angle_step: float
    center: Vec3 = (0.0, 0.0, 0.0)


Step = Annotated[
    Union[BoxStep, CylinderStep, PrismStep, RevolveStep, EdgeStep, RepeatStep, RepeatAroundStep],
    Field(discriminator="op"),
]
OPS = ("box", "cylinder", "prism", "revolve", "fillet", "chamfer", "repeat", "repeat_around")


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: Vec3 | None = None
    bodies: int | None = 1


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    steps: list[Step] = Field(min_length=1, max_length=200)
    expect: Expect | None = None


# ---------------------------------------------------------------- parsing
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")


def loads_lenient(text: str) -> Any:
    """json.loads that forgives what models often add: code fences, prose around the JSON,
    // comment lines and trailing commas."""
    raw = (text or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    cleaned = _FENCE.sub("", raw)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]
    cleaned = _LINE_COMMENT.sub("", cleaned)
    cleaned = _TRAILING_COMMA.sub(r"\1", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SwError(
            Code.BAD_ARGUMENT,
            f"The plan is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}.",
            'Send only the JSON object, like {"steps": [ ... ]}, with double quotes around names.',
        ) from None


def _where(loc: tuple, data: Any) -> str:
    parts = []
    for i, key in enumerate(loc):
        if key == "steps" and i + 1 < len(loc) and isinstance(loc[i + 1], int):
            continue
        if isinstance(key, int) and i > 0 and loc[i - 1] == "steps":
            op = None
            try:
                op = data["steps"][key].get("op")
            except Exception:  # noqa: BLE001
                pass
            parts.append(f"step {key + 1}" + (f" ({op})" if op else ""))
        elif isinstance(key, str) and key not in OPS:
            parts.append(key)
    return ", ".join(parts) or "plan"


def parse_plan(text: str) -> Plan:
    data = loads_lenient(text)
    if isinstance(data, list):
        data = {"steps": data}  # a bare list of steps is fine
    try:
        return Plan.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors()[:8]:
            msg = err["msg"]
            if err["type"] == "union_tag_invalid":
                msg = f"unknown op. Use one of: {', '.join(OPS)}"
            elif err["type"] == "union_tag_not_found":
                msg = f'every step needs "op": one of {", ".join(OPS)}'
            elif err["type"] == "extra_forbidden":
                msg = "this field is not allowed (check the spelling)"
            problems.append(f"{_where(err['loc'], data)}: {msg}")
        raise SwError(
            Code.BAD_ARGUMENT,
            "The plan has mistakes: " + "; ".join(problems) + ".",
            "Fix those steps and send the whole plan again. Nothing was built.",
        ) from None


# ---------------------------------------------------------------- dry run
def shape_of(step: Any) -> m.Shape | m.Revolve | None:
    cut = getattr(step, "mode", "add") == "cut"
    shape: m.Shape | None = None
    if isinstance(step, BoxStep):
        shape = m.box_shape(*step.x, *step.y, *step.z, cut)
    elif isinstance(step, CylinderStep):
        shape = m.cylinder_shape(*step.start, *step.end, step.diameter, cut)
    elif isinstance(step, PrismStep):
        pts = "; ".join(f"{a},{b}" for a, b in step.points)
        shape = m.prism_shape(step.axis, pts, step.start, step.end, cut)
    elif isinstance(step, RevolveStep):
        return m.revolve_spec(step.axis, step.center, step.profile, step.angle, cut)
    rot = getattr(step, "rotate", None)
    if shape is not None and rot is not None and abs(rot.deg) > 1e-9:
        if shape.tilt is not None:
            raise SwError(Code.BAD_ARGUMENT, "This cylinder is already slanted by its start and end, and it has a rotate too.",
                          "Use one of them: either a straight start/end plus rotate, or a slanted start/end alone.")
        shape.tilt = (rot.axis, rot.deg, tuple(rot.about))
    return shape


def check_plan(plan: Plan) -> list[m.Shape | m.Revolve | None]:
    """Validate geometry without SolidWorks. Returns the shape for each step (None otherwise)."""
    shapes: list[m.Shape | m.Revolve | None] = []
    group = 0
    tilted = False
    for i, step in enumerate(plan.steps, 1):
        try:
            shape = shape_of(step)
        except SwError as err:
            raise SwError(Code.BAD_ARGUMENT, f"Step {i} ({step.op}): {err.message}",
                          f"{err.fix} Nothing was built; send the corrected plan.") from None
        if isinstance(shape, m.Revolve):
            group, tilted = 0, False  # a revolve cannot be repeated
        elif shape is not None:
            group, tilted = 1, shape.tilt is not None
        elif isinstance(step, (RepeatStep, RepeatAroundStep)):
            if group == 0:
                raise SwError(Code.BAD_ARGUMENT, f"Step {i} ({step.op}) has nothing to repeat.",
                              "Put a box, cylinder or prism step before it.")
            if isinstance(step, RepeatAroundStep) and tilted:
                raise SwError(Code.BAD_ARGUMENT, f"Step {i} (repeat_around) cannot repeat a tilted shape.",
                              "Use a row repeat, or write each tilted copy as its own step.")
            if group * step.copies > m.MAX_COPIES:
                raise SwError(Code.BAD_ARGUMENT,
                              f"Step {i} ({step.op}) would make {group * step.copies} shapes; the limit is {m.MAX_COPIES}.",
                              "Use fewer copies or split the repeat.")
            if isinstance(step, RepeatStep) and sum(abs(v) for v in step.step) < 0.001:
                raise SwError(Code.BAD_ARGUMENT, f"Step {i} (repeat) has a zero step.", "Give a non-zero step.")
            if isinstance(step, RepeatAroundStep) and (
                abs(step.angle_step) < 0.01 or abs(step.angle_step) * step.copies > 360.001
            ):
                raise SwError(Code.BAD_ARGUMENT, f"Step {i} (repeat_around) has an invalid angle.",
                              "For N evenly spaced items use copies N-1 and angle_step 360/N.")
            group += group * step.copies
        shapes.append(shape)
    if all(s is None for s in shapes):
        raise SwError(Code.BAD_ARGUMENT, "The plan has no box, cylinder, prism or revolve step.",
                      "Start with the base body, e.g. a box.")
    return shapes


# ---------------------------------------------------------------- execution
_unsaved_failed_builds: list[str] = []


def _close_failed_builds(app: Any) -> None:
    while _unsaved_failed_builds:
        title = _unsaved_failed_builds.pop()
        try_call(app, "CloseDoc", title)


def execute(app: Any, doc: Any | None, plan: Plan, save_as: str = "", keep_open: bool = False) -> dict:
    """Build the plan. With doc=None a new part is created first. save_as="project/part"
    saves the finished part into the projects folder."""
    shapes = check_plan(plan)
    if save_as.strip():
        project.split_name(save_as)  # reject a bad name before building anything
    _close_failed_builds(app)
    created = doc is None
    if created:
        doc = m.create_part(app)
    modeler = m.Modeler(app, doc)
    title = try_call(doc, "GetTitle")
    volume_before = modeler.volume_mm3()
    log: list[str] = []
    pieces = len(modeler.bodies())
    split_by: list[str] = []  # steps after which the part had more separate pieces
    for i, (step, shape) in enumerate(zip(plan.steps, shapes), 1):
        try:
            if isinstance(shape, m.Revolve):
                log.append(modeler.revolve(shape)["feature"])
            elif shape is not None:
                out = modeler.build(shape)
                log.append(out["feature"])
            elif isinstance(step, EdgeStep):
                log.append(modeler.finish_edges(step.op, step.size, step.edges)["feature"])
            elif isinstance(step, RepeatStep):
                log.extend(modeler.repeat_linear(step.copies, *step.step)["features"])
            elif isinstance(step, RepeatAroundStep):
                log.extend(modeler.repeat_around(step.copies, step.angle_step, step.center)["features"])
        except SwError as err:
            if created and title:
                _unsaved_failed_builds.append(title)
            done = f" Steps 1-{i - 1} were built ({', '.join(log)})." if i > 1 else ""
            raise SwError(
                err.code,
                f"Step {i} ({step.op}) failed: {err.message}{done}",
                f"{err.fix} Then send the whole corrected plan again; it is rebuilt in a fresh part."
                if created else f"{err.fix} The steps before it stay in the part.",
                infra=err.infra, retryable=False, reconnect=err.reconnect,
            ) from None
        now = len(modeler.bodies())
        if now > pieces and shape is not None:
            mode = getattr(step, "mode", "add")
            split_by.append(f"step {i} ({step.op} {mode}) " + ("cut the part apart" if mode == "cut"
                                                              else "does not touch the rest of the part"))
        pieces = now
    if plan.name and created:
        try_call(doc, "SetTitle2", plan.name)
    summary = modeler.summary()
    out: dict[str, Any] = {"part": try_call(doc, "GetTitle"), "steps_built": len(plan.steps),
                           "features": len(log), **summary,
                           "volume_change_mm3": round(summary["volume_mm3"] - volume_before, 1)}
    problems = []
    exp = plan.expect
    if exp and exp.size and "size_mm" in summary:
        off = [round(summary["size_mm"][k] - exp.size[k], 2) for k in range(3)]
        if any(abs(d) > 0.5 for d in off):
            problems.append(f"size is {summary['size_mm']} but the plan expected {list(exp.size)}")
    if exp and exp.bodies is not None and summary.get("bodies") != exp.bodies:
        problems.append(f"{summary.get('bodies')} bodies, expected {exp.bodies}")
    wanted_bodies = exp.bodies if exp and exp.bodies is not None else 1
    if summary.get("bodies", 1) > wanted_bodies:
        if created and title:
            _unsaved_failed_builds.append(try_call(doc, "GetTitle") or title)
        where = "; ".join(split_by) if split_by else "a shape does not overlap the others"
        raise SwError(
            Code.CHECK_FAILED,
            f"The part came out in {summary['bodies']} separate pieces, so it was not saved: {where}. "
            f"Size {summary.get('size_mm')}, from {summary.get('min_mm')} to {summary.get('max_mm')}.",
            "Every added shape must overlap the part by at least 1 mm (a crank pin must reach into its web, "
            "a web into the shaft), and a cut must not slice the part in two. Fix those steps and send the "
            "whole plan again. If separate bodies are wanted, set \"expect\": {\"bodies\": N}.",
        )
    if problems:
        out["check"] = "MISMATCH: " + "; ".join(problems) + ". Find the step with the wrong numbers and rebuild."
    else:
        out["check"] = "matches the plan" if exp else "no expect given"
    if save_as.strip():
        out.update(project.save_part(app, doc, save_as))
        if created and not keep_open:
            try_call(app, "CloseDoc", try_call(doc, "GetTitle"))  # saved, so closing loses nothing
            out["window"] = "closed (the part is saved in the project)"
        out["next"] = "Saved in the project. Build the next part, or call make_assembly when all parts are done."
    else:
        out["next"] = "Tell the user what was built. Save with save_document if they want to keep it."
    return out
