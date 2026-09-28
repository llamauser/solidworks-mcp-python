from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.resilience import Session, sw_tool
from ..sw import dimensions as dims


@sw_tool(needs="model", idempotent=True, timeout=60)
def set_dimension(
    sw: Session,
    dimension_name: Annotated[
        str,
        Field(description='Exact name from get_selection_context, e.g. "D1@Boss-Extrude1".'),
    ],
    new_value: Annotated[
        float,
        Field(gt=0, description="New value: millimeters for lengths, degrees for angles."),
    ],
) -> dict:
    """Change one dimension of the open part or assembly, then rebuild.

    Use when: the user wants something longer, thicker, wider, deeper, or a hole bigger.
    Get the name first with get_selection_context. If the rebuild fails,
    the old value is put back automatically. Save afterwards with save_document.
    Example: set_dimension(dimension_name="D1@Boss-Extrude1", new_value=25)
    """
    return dims.set_dimension(sw.doc, dimension_name, new_value)


def register(mcp) -> None:
    mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(title="Change a dimension", destructive_hint=False, idempotent_hint=True),
    )(set_dimension)
