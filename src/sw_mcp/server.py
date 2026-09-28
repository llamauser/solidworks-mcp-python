"""Build the MCP server. Importing this module never touches SolidWorks (OpenCode only
waits a few seconds for the tool list), the connection is made on the first tool call."""

from __future__ import annotations

from mcp.server import MCPServer

from . import __version__, config
from .tools import register_all

INSTRUCTIONS = """\
Tools to build, inspect and edit parts in SolidWorks. All lengths are millimeters, angles degrees.
World axes: X right, Y up, Z toward the viewer. Build: new_part, then make_box / make_cylinder /
make_prism (mode add or cut), finish_edges, and check with get_model_summary.
Edit: ask the user to click, get_selection_context, set_dimension. Save with save_document.
Every tool answers JSON with "ok". If ok is false, read "fix" and follow it.
"""


def _slim_schemas(mcp: MCPServer) -> None:
    """Drop pydantic's auto 'title' fields ("X Min Mm"): pure token cost, no information.

    The tool list is re-sent to the model on every request, so this matters on free tiers.
    """
    manager = getattr(mcp, "_tool_manager", None)
    for tool in getattr(manager, "_tools", {}).values():
        params = tool.parameters
        params.pop("title", None)
        for prop in params.get("properties", {}).values():
            prop.pop("title", None)


def create_server() -> MCPServer:
    instructions = INSTRUCTIONS if config.SEND_INSTRUCTIONS else None
    mcp = MCPServer("solidworks", instructions=instructions, version=__version__)
    register_all(mcp)
    _slim_schemas(mcp)
    return mcp


mcp = create_server()
