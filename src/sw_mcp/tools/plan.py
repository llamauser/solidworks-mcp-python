from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.resilience import Session, sw_tool
from ..sw import plan as p


@sw_tool(needs="app", timeout=900)
def build_part(
    sw: Session,
    plan: Annotated[str, Field(description='JSON: {"steps": [...], "expect": {"size": [x, y, z]}}')],
    start_new_part: Annotated[bool, Field(description="false = add the steps to the part that is open now.")] = True,
    save_as: Annotated[str, Field(description='"project/part" to save it for an assembly, e.g. "V4 engine/piston".')] = "",
    keep_open: Annotated[bool, Field(description="With save_as: keep the part window open (default closes it).")] = False,
) -> dict:
    """Build a whole part from one JSON plan. Fewest requests; use it for anything new.

    Use when: the user describes a part. Write the full plan first, then call this once.
    Units mm. Axes: X right, Y up, Z toward the viewer. Base on y=0, centered on x=0, z=0.
    Step kinds:
    {"op":"box","mode":"add|cut","x":[min,max],"y":[min,max],"z":[min,max]}
    {"op":"cylinder","mode":"add|cut","start":[x,y,z],"end":[x,y,z],"diameter":d}  (along X, Y or Z)
    {"op":"prism","mode":"add|cut","axis":"z","points":[[a,b],...],"start":s,"end":e}  (points: (x,y) for z, (x,z) for y, (y,z) for x)
    {"op":"revolve","mode":"add|cut","axis":"y","center":[0,0,0],"profile":[[r,h],...],"angle":360}  (r = distance from the axis, h = position along it)
    {"op":"fillet|chamfer","size":r,"edges":"vertical|top|bottom|parallel_x|parallel_z|circular|all"}
    {"op":"repeat","copies":n,"step":[dx,dy,dz]}  {"op":"repeat_around","copies":n,"angle_step":deg,"center":[x,y,z]}
    Tilt a box/cylinder/prism: add "rotate":{"axis":"z","deg":45,"about":[x,y,z]} (right-hand rule).
    Repeats copy the last shape, or the whole last repeated group. Fillets before pockets and holes.
    Example: {"steps":[{"op":"box","x":[-30,30],"y":[0,10],"z":[-20,20]},{"op":"fillet","size":5,"edges":"vertical"},
    {"op":"cylinder","mode":"cut","start":[-22,-1,-12],"end":[-22,11,-12],"diameter":6},
    {"op":"repeat","copies":1,"step":[44,0,0]},{"op":"repeat","copies":1,"step":[0,0,24]}],"expect":{"size":[60,10,40]}}
    On error: fix the step it names and send the whole plan again.
    """
    return p.execute(sw.app, None if start_new_part else sw.doc, p.parse_plan(plan), save_as, keep_open)


def register(mcp) -> None:
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Build part from plan",
                                                                   destructive_hint=False))(build_part)
