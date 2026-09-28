from __future__ import annotations

from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.resilience import Session, sw_tool
from ..sw import engine as eng
from ..sw import mechanism as mech


@sw_tool(needs="assembly", timeout=300)
def connect_parts(
    sw: Session,
    fixed_part: Annotated[str, Field(description='Part that stays still (e.g. "block"). Empty = the biggest part.')] = "",
) -> dict:
    """Turn the active assembly into a mechanism: join every shaft to the bore it sits in.

    Use when: an assembly should move (engine, gearbox, hinge). Parts must share exact axes:
    a cylinder of one part inside a hole/cylinder of another (radius within 1 mm).
    Parts with no joint are fixed where they are. Run it once per assembly.
    Example: connect_parts(fixed_part="block")
    """
    return mech.connect_parts(sw.doc, fixed_part)


@sw_tool(needs="assembly", timeout=600)
def move_mechanism(
    sw: Session,
    part: Annotated[str, Field(description='The part to turn, e.g. "crankshaft".')],
    degrees: Annotated[float, Field(ge=-3600, le=3600, description="How far to turn it (360 = one full turn).")] = 360.0,
    steps: Annotated[int, Field(ge=1, le=180, description="Frames shown in SolidWorks while turning.")] = 36,
) -> dict:
    """Turn one part around its joint, live in SolidWorks, and report how the other parts moved.

    Use when: showing or checking that a mechanism works (after connect_parts).
    Returns each moving part's travel (e.g. a piston's stroke) and which parts did not move.
    Example: move_mechanism(part="crankshaft", degrees=360)
    """
    return mech.move_mechanism(sw.app, sw.doc, part, degrees, steps)


@sw_tool(needs="assembly", timeout=600)
def make_motion_study(
    sw: Session,
    part: Annotated[str, Field(description='The part the motor turns, e.g. "crankshaft".')],
    rpm: Annotated[float, Field(gt=0, le=100000, description="Motor speed in revolutions per minute.")] = 60.0,
    seconds: Annotated[float, Field(gt=0, le=600, description="Length of the study.")] = 5.0,
    kind: Annotated[Literal["animation", "basic"], Field(description="animation = kinematic; basic = Basic Motion (physics).")] = "animation",
) -> dict:
    """Create a SolidWorks Motion Study with a rotary motor on a part, calculate it and play it.

    Use when: the user wants a motion study / animation they can replay or save as a video.
    Needs joints first (connect_parts). If SolidWorks refuses a step, it says which one.
    Example: make_motion_study(part="crankshaft", rpm=60, seconds=5)
    """
    return mech.make_motion_study(sw.doc, part, rpm, seconds, kind)


@sw_tool(needs="app", timeout=1800)
def make_engine(
    sw: Session,
    project: Annotated[str, Field(description='Project (folder) name, e.g. "V4 engine". Use a new name for a new engine.')],
    layout: Annotated[Literal["inline", "v", "boxer"], Field(description="inline, v or boxer (flat).")] = "v",
    cylinders: Annotated[int, Field(ge=1, le=8, description="1-8; v and boxer need an even number.")] = 4,
    bank_angle: Annotated[float, Field(ge=30, le=150, description="V angle in degrees (v only).")] = 90.0,
    bore: Annotated[float, Field(ge=20, le=400, description="Cylinder diameter, mm.")] = 80.0,
    stroke: Annotated[float, Field(gt=0, le=640, description="Piston travel, mm (0.4-1.6 x bore).")] = 70.0,
    heads: Annotated[bool, Field(description="Add cylinder heads (they hide the pistons from above).")] = True,
) -> dict:
    """Build a complete, working piston engine: block, crankshaft, rods, pistons and heads,
    assembled and connected so it can turn. One call; takes a few minutes in SolidWorks.

    Use when: the user asks for an engine (V4, V8, inline 4, single cylinder, boxer ...).
    Do NOT model engines with build_part: this tool gets every axis exactly right.
    Afterwards: move_mechanism(part="crankshaft") or make_motion_study(part="crankshaft").
    Example: make_engine(project="V4 engine", layout="v", cylinders=4, bank_angle=90)
    """
    return eng.build(sw.app, project, eng.design(layout, cylinders, bank_angle, bore, stroke, 0.0, heads))


def register(mcp) -> None:
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Make engine"))(make_engine)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Connect parts"))(connect_parts)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Move mechanism"))(move_mechanism)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Motion study"))(make_motion_study)
