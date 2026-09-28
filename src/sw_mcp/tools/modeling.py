from __future__ import annotations

from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.com_utils import try_call
from ..core.errors import Code, SwError
from ..core.resilience import Session, sw_tool
from ..sw import modeling as m

Mode = Annotated[Literal["add", "cut"], Field(description="add = new material, cut = remove material (holes, pockets).")]


def _mm(desc: str) -> type:
    return Annotated[float, Field(description=desc)]  # type: ignore[return-value]


@sw_tool(needs="app", timeout=60)
def new_part(sw: Session) -> dict:
    """Create a new empty part and make it the active window.

    Use when: the user asks to build or design something new.
    World axes in every modeling tool: X right, Y up, Z toward the viewer, all in mm.
    Example: new_part()
    """
    return m.new_part(sw.app)


@sw_tool(needs="part", timeout=150)
def make_box(
    sw: Session,
    mode: Mode,
    x_min_mm: _mm("Left side (X)."), x_max_mm: _mm("Right side (X)."),
    y_min_mm: _mm("Bottom (Y, up)."), y_max_mm: _mm("Top (Y)."),
    z_min_mm: _mm("Back (Z)."), z_max_mm: _mm("Front (Z)."),
) -> dict:
    """Add or cut a rectangular block between min and max world coordinates (mm).

    Use when: base plates, blocks, ribs, slots, pockets, notches.
    Put the base on Y=0 and center it on X=0, Z=0.
    Example, plate 60x40, 10 thick: make_box(mode="add", x_min_mm=-30, x_max_mm=30,
      y_min_mm=0, y_max_mm=10, z_min_mm=-20, z_max_mm=20)
    Pocket 4 deep in its top: make_box(mode="cut", ..., y_min_mm=6, y_max_mm=10, ...)
    """
    return m.Modeler(sw.app, sw.doc).build(m.box_shape(x_min_mm, x_max_mm, y_min_mm, y_max_mm, z_min_mm, z_max_mm,
                                                        mode == "cut"))


@sw_tool(needs="part", timeout=150)
def make_cylinder(
    sw: Session,
    mode: Mode,
    start_x_mm: _mm("Center of one end (X)."), start_y_mm: _mm("Center of one end (Y)."),
    start_z_mm: _mm("Center of one end (Z)."),
    end_x_mm: _mm("Center of the other end (X)."), end_y_mm: _mm("Center of the other end (Y)."),
    end_z_mm: _mm("Center of the other end (Z)."),
    diameter_mm: Annotated[float, Field(gt=0, description="Diameter in mm.")],
) -> dict:
    """Add a round rod/boss, or cut a round hole, between two end centers (mm).

    It must run along X, Y or Z: only one coordinate may differ between start and end.
    Use when: holes, bores, pins, round bosses, shafts.
    For a through hole, run it from below the part to above it.
    Example, 6 mm hole through a 10 mm plate at x=20, z=-10:
      make_cylinder(mode="cut", start_x_mm=20, start_y_mm=-1, start_z_mm=-10,
                    end_x_mm=20, end_y_mm=11, end_z_mm=-10, diameter_mm=6)
    """
    return m.Modeler(sw.app, sw.doc).build(m.cylinder_shape(start_x_mm, start_y_mm, start_z_mm,
                                                             end_x_mm, end_y_mm, end_z_mm, diameter_mm,
                                                             mode == "cut"))


@sw_tool(needs="part", timeout=150)
def make_prism(
    sw: Session,
    mode: Mode,
    axis: Annotated[Literal["x", "y", "z"], Field(description="Direction the outline is pushed along.")],
    points_mm: Annotated[str, Field(description='Outline corners in order: "a,b; a,b; ...". For axis z use (x,y), for y use (x,z), for x use (y,z).')],
    start_mm: _mm("Where the shape starts along the axis."),
    end_mm: _mm("Where the shape ends along the axis."),
) -> dict:
    """Add or cut any straight-sided outline (L, T, U, triangle, hexagon) pushed along an axis.

    Use when: the shape is not a plain box or cylinder, e.g. an L-bracket or a slanted cut.
    Example, L-bracket profile seen from the front, 30 mm deep:
      make_prism(mode="add", axis="z", points_mm="0,0; 50,0; 50,5; 5,5; 5,40; 0,40",
                 start_mm=-15, end_mm=15)
    """
    return m.Modeler(sw.app, sw.doc).build(m.prism_shape(axis, points_mm, start_mm, end_mm, mode == "cut"))


@sw_tool(needs="part", timeout=600)
def repeat_last_shape(
    sw: Session,
    copies: Annotated[int, Field(ge=1, le=m.MAX_COPIES, description="How many NEW copies to add.")],
    step_x_mm: _mm("Shift between copies along X.") = 0.0,
    step_y_mm: _mm("Shift between copies along Y.") = 0.0,
    step_z_mm: _mm("Shift between copies along Z.") = 0.0,
) -> dict:
    """Copy the last shape in a straight row. After a repeat, the next repeat copies the whole
    group, so two calls make a grid.

    Use when: rows or grids of holes, ribs, fins, slots.
    Each copy is shifted by the step from the previous one. Cuts stay cuts.
    Example, 4 corner holes: make the hole at x=-22 z=-12, then
      repeat_last_shape(copies=1, step_x_mm=44), then repeat_last_shape(copies=1, step_z_mm=24)
    """
    modeler = m.Modeler(sw.app, sw.doc)
    return modeler.repeat_linear(copies, step_x_mm, step_y_mm, step_z_mm)


@sw_tool(needs="part", timeout=600)
def repeat_last_shape_around(
    sw: Session,
    copies: Annotated[int, Field(ge=1, le=m.MAX_COPIES, description="How many NEW copies to add.")],
    angle_step_deg: _mm("Angle between neighbours in degrees (360/N for N evenly spaced)."),
    center_x_mm: _mm("Circle center X.") = 0.0,
    center_y_mm: _mm("Circle center Y.") = 0.0,
    center_z_mm: _mm("Circle center Z.") = 0.0,
) -> dict:
    """Copy the last shape (or last repeated group) around a circle.

    Use when: bolt circles, spokes, fins, holes around a flange. The circle turns around a line through the center, parallel to the last shape's axis
    (a hole drilled along Y turns around a vertical line).
    Example, 6 holes on a 70 mm bolt circle: make the hole at x=35, z=0 along Y, then
      repeat_last_shape_around(copies=5, angle_step_deg=60)
    """
    modeler = m.Modeler(sw.app, sw.doc)
    return modeler.repeat_around(copies, angle_step_deg, (center_x_mm, center_y_mm, center_z_mm))


@sw_tool(needs="part", timeout=120)
def finish_edges(
    sw: Session,
    kind: Annotated[Literal["fillet", "chamfer"], Field(description="fillet = rounded, chamfer = 45 degree bevel.")],
    size_mm: Annotated[float, Field(gt=0, description="Fillet radius or chamfer size in mm.")],
    edges: Annotated[
        Literal[m.EDGE_FILTERS],  # type: ignore[valid-type]
        Field(description="vertical = corners along Y; top/bottom = edges on the highest/lowest face; "
                          "parallel_x/parallel_z = straight edges along X/Z; circular = hole and boss rims."),
    ],
) -> dict:
    """Round (fillet) or bevel (chamfer) a group of edges of the part.

    Use when: "round the corners", "break the edges", "chamfer the holes".
    Do this AFTER the outer shape and BEFORE pockets and holes, or their edges get rounded too.
    Example: finish_edges(kind="fillet", size_mm=3, edges="vertical")
    """
    return m.Modeler(sw.app, sw.doc).finish_edges(kind, size_mm, edges)


@sw_tool(needs="part", timeout=60)
def undo_last_feature(sw: Session) -> dict:
    """Delete the most recent feature of the part (the last box, cylinder, prism or fillet).

    Use when: a step came out wrong and you want to redo it differently.
    Example: undo_last_feature()
    """
    modeler = m.Modeler(sw.app, sw.doc)
    modeler.exit_sketch()
    last = modeler.last_feature()
    kind = try_call(last, "GetTypeName2") or ""
    if last is None or kind in m.SYSTEM_FEATURE_TYPES:
        raise SwError(Code.NOT_FOUND, "There is no feature to undo.", "Build something first.")
    name = try_call(last, "Name")
    modeler.delete(last)
    if try_call(sw.doc, "FeatureByName", name) is not None:
        raise SwError(Code.SW_ERROR, f"SolidWorks did not delete {name}.", "Ask the user to delete it by hand.")
    modeler.zoom()
    return {"deleted": name, **modeler.summary()}


@sw_tool(needs="part", idempotent=True)
def get_model_summary(sw: Session) -> dict:
    """Overall size, volume and feature list of the active part, to check the result.

    Use when: after building, to compare the result with the request, or before planning edits.
    Returns size_mm [x,y,z], min_mm, max_mm, volume_mm3, bodies (should be 1), features.
    Example: get_model_summary()
    """
    modeler = m.Modeler(sw.app, sw.doc)
    return {"part": try_call(sw.doc, "GetTitle"), **modeler.summary(), "features": modeler.features()}


def register(mcp) -> None:
    build = ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="New part"))(new_part)
    mcp.tool(structured_output=False, annotations=build)(make_box)
    mcp.tool(structured_output=False, annotations=build)(make_cylinder)
    mcp.tool(structured_output=False, annotations=build)(make_prism)
    mcp.tool(structured_output=False, annotations=build)(repeat_last_shape)
    mcp.tool(structured_output=False, annotations=build)(repeat_last_shape_around)
    mcp.tool(structured_output=False, annotations=build)(finish_edges)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(destructive_hint=True))(undo_last_feature)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))(
        get_model_summary
    )
