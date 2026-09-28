"""Build the MCP server. Importing this module never touches SolidWorks (OpenCode only
waits a few seconds for the tool list), the connection is made on the first tool call."""

from __future__ import annotations

from mcp.server import MCPServer

from . import __version__
from .tools import register_all

INSTRUCTIONS = """\
Tools to inspect and edit the model open in SolidWorks.
Workflow: 1) get_status  2) open_document if needed  3) ask the user to click the face,
edge or dimension they mean  4) get_selection_context  5) set_dimension  6) save_document.
All lengths are millimeters, angles are degrees. Every tool answers JSON with "ok".
If ok is false, read "fix" and follow it. After any error, call get_status.
"""


def create_server() -> MCPServer:
    mcp = MCPServer("solidworks", instructions=INSTRUCTIONS, version=__version__)
    register_all(mcp)
    return mcp


mcp = create_server()
