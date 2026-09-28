"""Build the MCP server. Importing this module never touches SolidWorks (OpenCode only
waits a few seconds for the tool list), the connection is made on the first tool call."""

from __future__ import annotations

from importlib import resources

from mcp.server import MCPServer

from . import __version__, config
from .tools import register_all


def load_guide() -> str:
    """The shared operating guide (src/sw_mcp/guide.md): the single source for every client."""
    return resources.files("sw_mcp").joinpath("guide.md").read_text(encoding="utf-8")


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
    instructions = load_guide() if config.SEND_INSTRUCTIONS else None
    mcp = MCPServer("solidworks", instructions=instructions, version=__version__)
    register_all(mcp)
    _slim_schemas(mcp)
    return mcp


mcp = create_server()
