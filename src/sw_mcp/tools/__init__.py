"""MCP tool wrappers. Each module exposes register(mcp)."""

from . import context, dimensions, documents, modeling, session

MODULES = (session, documents, modeling, context, dimensions)


def register_all(mcp) -> None:
    for module in MODULES:
        module.register(mcp)
