from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.resilience import Session, sw_tool
from ..sw import documents as docs


@sw_tool(needs="app", idempotent=True, timeout=120)
def open_document(
    sw: Session,
    file_path: Annotated[
        str,
        Field(description=r"Full path of a .SLDPRT, .SLDASM or .SLDDRW file, e.g. C:\Parts\bracket.SLDPRT"),
    ],
) -> dict:
    """Open a SolidWorks part, assembly or drawing file and make it the active window.

    Use when: the user names a file to work on.
    If the file is already open, it is just brought to the front.
    Example: open_document(file_path="C:\\Parts\\bracket.SLDPRT")
    """
    return docs.open_document(sw.app, file_path)


@sw_tool(needs="doc", idempotent=True, timeout=120)
def save_document(
    sw: Session,
    save_as_path: Annotated[
        str,
        Field(
            description=(
                "Leave empty to save the file in place. Or a full path to save a copy under a new name. "
                "The extension picks the format: .SLDPRT/.SLDASM/.SLDDRW, or export to .step .stl .pdf .dxf .igs .x_t"
            )
        ),
    ] = "",
    overwrite: Annotated[
        bool, Field(description="Only true if the user agreed to replace an existing file at save_as_path.")
    ] = False,
) -> dict:
    """Save the active document, or save/export it to another file.

    Use when: the user wants to keep changes, or wants a STEP/STL/PDF export.
    Examples: save_document()
              save_document(save_as_path="C:\\Exports\\bracket.step")
    """
    return docs.save_document(sw.doc, save_as_path, overwrite)


def register(mcp) -> None:
    mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(title="Open document", read_only_hint=False, idempotent_hint=True),
    )(open_document)
    mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(title="Save or export", destructive_hint=False, idempotent_hint=True),
    )(save_document)
