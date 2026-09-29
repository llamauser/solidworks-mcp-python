from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.errors import Code, SwError
from ..core.resilience import Session, sw_tool
from ..sw import design
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


@sw_tool(needs=None)
def plan_machine(
    sw: Session,
    project: Annotated[str, Field(description="A NEW project name for this machine, e.g. \"CVT 1\".")],
    goal: Annotated[str, Field(description="One or two sentences: what the machine is and does.")],
    parts: Annotated[str, Field(description='One part per line: "name: what it is, main sizes, where it sits".')],
    notes: Annotated[str, Field(description="Shared numbers every part must use: spacing, clearances.")] = "",
    references: Annotated[str, Field(description='Named axes/frames, one per line: "axis input through 0,0,0 along y", '
                                                 '"frame left_bank origin 0,0,0 turn x 45".')] = "",
) -> dict:
    """Write the plan of a machine with several parts: the checklist every AI model follows.

    Use when: starting a machine (anything with 2 or more parts). Call it FIRST, then build each part
    with build_part(save_as="<project>/<name>"). Name every shaft axis in references: parts then use
    {"op":"cylinder","on_axis":"input","from":0,"to":200,"diameter":30} and line up exactly.
    A frame (origin + turn) lets a tilted part (a V-engine bank) be described upright.
    Calling it again replaces the plan but keeps parts that are already built.
    Example: plan_machine(project="CVT 1", goal="Belt CVT, shafts 250 mm apart",
             parts="input_shaft: d30 x 200 on axis input\nprimary_pulley: two cones d150 on axis input",
             references="axis input through 0,0,0 along y\naxis output through 250,0,0 along y",
             notes="bores 0.5 mm bigger than the shafts")
    """
    items = design.parse_parts(parts)
    if not items:
        raise SwError(Code.BAD_ARGUMENT, "The plan has no parts.",
                      'Give one part per line, like "input_shaft: d30 x 200 along Y at x=0".')
    pr.split_name(f"{project}/x")  # a usable project name
    design.set_plan(project, goal, items, notes)
    if references.strip():
        design.set_references(project, references)
    return {"project": project, "parts": [p["name"] for p in items], "design": design.summary(project),
            "next": f'Build the first part with build_part(save_as="{project}/{items[0]["name"]}").'}


def register(mcp) -> None:
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Plan a machine"))(plan_machine)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Make assembly"))(make_assembly)
    mcp.tool(structured_output=False, annotations=ToolAnnotations(read_only_hint=True))(list_project)
