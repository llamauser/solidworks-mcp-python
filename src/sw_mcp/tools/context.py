from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.resilience import Session, sw_tool
from ..sw.geometry import selection_context


@sw_tool(needs="doc", idempotent=True)
def get_selection_context(
    sw: Session,
    max_items: Annotated[int, Field(ge=1, le=20, description="How many selected items to describe.")] = 5,
) -> dict:
    """Describe what the user has selected (clicked) in SolidWorks, as short JSON.

    Use when: the user says "this face", "this hole", "this edge", "make it thicker".
    Call it BEFORE any tool that changes the model, because changes clear the selection.
    Returns items like:
      face: surface (plane/cylinder), normal (points out of the part), radius_mm, area_mm2, feature, dims
      edge: kind (line/arc/circle), length_mm, radius_mm
      dimension: name, value, unit      mate: mate_type, components
    Each "dims" entry has a name like "D1@Boss-Extrude1" to pass to set_dimension.
    Units: mm, mm2, degrees. Example: get_selection_context()
    """
    return selection_context(sw.doc, max_items)


def register(mcp) -> None:
    mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(title="Read selection", read_only_hint=True, idempotent_hint=True),
    )(get_selection_context)
