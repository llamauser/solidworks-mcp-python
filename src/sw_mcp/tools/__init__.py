"""MCP tool wrappers. Each module exposes register(mcp)."""

from . import context, dimensions, documents, session

MODULES = (session, documents, context, dimensions)


def register_all(mcp) -> None:
    for module in MODULES:
        module.register(mcp)
