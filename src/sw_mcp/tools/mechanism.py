from __future__ import annotations

from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.resilience import Session, sw_tool
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


def register(mcp) -> None:
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Connect parts"))(connect_parts)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Move mechanism"))(move_mechanism)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Motion study"))(make_motion_study)
