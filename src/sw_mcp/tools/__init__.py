"""MCP tool wrappers. Each module exposes register(mcp)."""

from . import context, dimensions, documents, modeling, plan, session

MODULES = (session, documents, plan, modeling, context, dimensions)


def register_all(mcp) -> None:
    for module in MODULES:
        module.register(mcp)
