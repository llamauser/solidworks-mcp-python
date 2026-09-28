from __future__ import annotations

from mcp.types import ToolAnnotations

from ..core import connection
from ..core.com_utils import call, try_call
from ..core.resilience import Session, runtime, sw_tool
from ..sw.documents import active_sketch_name, doc_summary


@sw_tool(needs=None, recovery=True, idempotent=True, timeout=45)
def get_status(sw: Session) -> dict:
    """Check SolidWorks and see what is open. Starts SolidWorks if it is closed.

    Use when: at the start of every task, and after ANY error.
    Returns the SolidWorks version, the active document (name, type, path,
    unsaved_changes) and the active sketch.
    If the answer is SW_STARTING, wait about 30 seconds and call get_status again.
    Example: get_status()
    """
    app = sw.connect(allow_launch=True)
    revision = call(app, "RevisionNumber")
    doc = connection.active_doc(app)
    out: dict = {
        "solidworks": "ready",
        "version": connection.version_label(revision),
        "open_documents": try_call(app, "GetDocumentCount"),
        "active_document": doc_summary(doc) if doc is not None else None,
    }
    if doc is not None:
        sketch = active_sketch_name(doc)
        if sketch:
            out["active_sketch"] = sketch
        out["next"] = "Ask the user to click what they mean in SolidWorks, then call get_selection_context."
    else:
        out["next"] = "No file is open. Call open_document with a full file path."
    if runtime.breaker.state != "closed":
        out["connection"] = runtime.breaker.snapshot()
    return out


def register(mcp) -> None:
    mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(title="SolidWorks status", read_only_hint=True, idempotent_hint=True),
    )(get_status)
