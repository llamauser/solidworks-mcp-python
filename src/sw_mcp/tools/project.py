from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.resilience import Session, sw_tool
from ..sw import project as pr


@sw_tool(needs="app", timeout=600)
def make_assembly(
    sw: Session,
    project: Annotated[str, Field(description="Project name used in build_part save_as, e.g. \"V4 engine\".")],
    parts: Annotated[str, Field(description='"all", or part names separated by commas.')] = "all",
    name: Annotated[str, Field(description="File name for the assembly.")] = "Assembly",
) -> dict:
    """Put the saved parts of a project together into one assembly.

    Use when: every part of a machine is built and saved with build_part(save_as="project/part").
    Parts are modeled in the machine's own coordinates, so they are placed at the origin
    as they are: no mates needed.
    Example: make_assembly(project="V4 engine", parts="all", name="V4 engine")
    """
    return pr.make_assembly(sw.app, project, parts, name)


@sw_tool(needs=None, idempotent=True)
def list_project(
    sw: Session,
    project: Annotated[str, Field(description="Project name; empty lists all projects.")] = "",
) -> dict:
    """List the parts and assemblies saved in a project (or all projects).

    Use when: continuing a multi-part job, to see which parts are already done.
    Example: list_project(project="V4 engine")
    """
    return pr.list_project(project)


def register(mcp) -> None:
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Make assembly"))(make_assembly)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(read_only_hint=True))(list_project)
