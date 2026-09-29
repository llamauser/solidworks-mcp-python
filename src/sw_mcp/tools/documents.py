from __future__ import annotations

from typing import Annotated, Literal

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


@sw_tool(needs="app", idempotent=False, timeout=120)
def manage_documents(
    sw: Session,
    action: Annotated[Literal["list", "activate", "close"], Field(description="list, activate (bring to front) or close.")],
    name: Annotated[str, Field(description='Document name. For close also: "" = the active window, "all", '
                                           '"others" = all but the active one, "new" = never-saved parts.')] = "",
    discard_unsaved: Annotated[bool, Field(description="Only true if the user agreed to lose unsaved changes.")] = False,
) -> dict:
    """List, bring to the front, or close SolidWorks windows.

    Use when: too many windows are open, or a specific part/assembly should be the active one.
    Closing never loses work unless discard_unsaved=true: unsaved documents are reported instead.
    Examples: manage_documents(action="list")
              manage_documents(action="activate", name="V4 engine")
              manage_documents(action="close", name="others")
              manage_documents(action="close", name="new", discard_unsaved=true)  (only if the user agreed)
    """
    if action == "list":
        return docs.list_documents(sw.app)
    if action == "activate":
        return docs.activate_document(sw.app, name)
    return docs.close_documents(sw.app, name, discard_unsaved)


@sw_tool(needs="doc", idempotent=True, timeout=120)
def save_picture(
    sw: Session,
    file_path: Annotated[str, Field(description=r"Where to save the image, e.g. C:\Temp\result.png")],
) -> dict:
    """Save a picture of the active part or assembly (isometric, zoomed to fit).

    Use when: a picture of the result is wanted (the assistant records one after each build).
    Example: save_picture(file_path="C:\\Temp\\result.png")
    """
    return docs.save_picture(sw.doc, file_path)


def register(mcp) -> None:
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Save picture", read_only_hint=True))(
        save_picture)
    mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(title="Open document", read_only_hint=False, idempotent_hint=True),
    )(open_document)
    mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(title="Save or export", destructive_hint=False, idempotent_hint=True),
    )(save_document)
    mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(title="Manage windows", destructive_hint=True),
    )(manage_documents)
