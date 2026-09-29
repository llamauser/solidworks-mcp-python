"""The shared design picture (design.json): kept by the tools, handed to every model."""

from __future__ import annotations

import json

import pytest

from sw_mcp.core import connection
from sw_mcp.core.errors import Code
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import design
from sw_mcp.sw import modeling as m
from sw_mcp.sw import plan as p
from sw_mcp.tools.mechanism import connect_parts, make_engine
from sw_mcp.tools.plan import build_part
from sw_mcp.tools.project import list_project, make_assembly, plan_machine
from tests.conftest import parse

SHAFT = {"steps": [{"op": "cylinder", "start": [0, 0, 0], "end": [0, 200, 0], "diameter": 30}]}
PULLEY = {"steps": [{"op": "revolve", "axis": "y", "center": [0, 0, 0],
                     "profile": [[15.25, 80], [75, 80], [75, 110], [15.25, 110]]}]}


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_last_group", {})
    monkeypatch.setattr(p, "_unsaved_failed_builds", [])
    monkeypatch.setenv("SW_MCP_PROJECTS", str(tmp_path / "projects"))
    fake = make_modeling_app()
    connection.use_app_factory(lambda: fake)
    yield fake
    connection.use_app_factory(None)


def test_plan_machine_writes_the_checklist(app):
    out = parse(plan_machine(project="CVT 1", goal="Belt CVT, shafts 250 mm apart",
                             parts="input_shaft: d30 x 200 along Y at x=0\n- pulley: cone sheaves d150 on the shaft",
                             notes="input axis Y through x=0, z=0"))
    assert out["ok"] and out["parts"] == ["input_shaft", "pulley"]
    assert "Checklist: input_shaft todo" in out["design"] and "Next: input_shaft" in out["design"]
    assert parse(plan_machine(project="CVT 1", goal="x", parts=""))["error"] == Code.BAD_ARGUMENT


def test_builds_fill_the_design_and_keep_the_checklist(app):
    parse(plan_machine(project="CVT 1", goal="Belt CVT", parts="input_shaft: d30\npulley: d150\nbelt: V belt"))
    assert parse(build_part(plan=json.dumps(SHAFT), save_as="CVT 1/input_shaft"))["ok"]
    assert parse(build_part(plan=json.dumps(PULLEY), save_as="CVT 1/pulley"))["ok"]
    bad = {"steps": [{"op": "box", "x": [0, 10], "y": [0, 1], "z": [0, 10]},
                     {"op": "cylinder", "start": [0, 50, 0], "end": [0, 60, 0], "diameter": 3}]}
    assert parse(build_part(plan=json.dumps(bad), save_as="CVT 1/belt"))["error"] == Code.CHECK_FAILED
    data = design.load("CVT 1")
    assert data["parts"]["input_shaft"]["round_features"] == ["shaft d30 from (0, 0, 0) to (0, 200, 0)"]
    assert "hole d30.5" in data["parts"]["pulley"]["round_features"][0]
    assert [x["status"] for x in data["plan"]["parts"]] == ["built", "built", "failed"]
    text = design.summary("CVT 1")
    assert "input_shaft built" in text and "belt FAILED" in text and "Next: belt" in text
    assert "shaft d30 from (0, 0, 0) to (0, 200, 0)" in text and "separate pieces" in text
    parse(plan_machine(project="CVT 1", goal="Belt CVT v2", parts="input_shaft: d30\nbelt: V belt"))
    assert [x["status"] for x in design.load("CVT 1")["plan"]["parts"]] == ["built", "failed"]  # kept


def test_assembly_and_joints_are_recorded(app):
    block = {"steps": [{"op": "box", "x": [-60, 60], "y": [0, 60], "z": [-30, 30]},
                       {"op": "cylinder", "mode": "cut", "start": [-61, 30, 0], "end": [61, 30, 0], "diameter": 20}]}
    crank = {"steps": [{"op": "cylinder", "start": [-70, 30, 0], "end": [70, 30, 0], "diameter": 20},
                       {"op": "box", "x": [62, 66], "y": [30, 50], "z": [-4, 4]}]}
    parse(build_part(plan=json.dumps(block), save_as="Mech/block"))
    parse(build_part(plan=json.dumps(crank), save_as="Mech/crank"))
    assert parse(make_assembly(project="Mech", name="Mech"))["ok"]
    assert parse(connect_parts(fixed_part="block"))["ok"]
    asm = design.load("Mech")["assembly"]
    assert asm["components"] == ["block", "crank"] and len(asm["joints"]) == 1 and asm["moving"] == ["crank-1"]
    assert "1 joints" in design.summary("Mech") and "moving: crank-1" in design.summary("Mech")
    assert "CURRENT DESIGN" in parse(list_project(project="Mech"))["design"]


def test_make_engine_writes_its_checklist(app):
    assert parse(make_engine(project="Single", layout="inline", cylinders=1, heads=False))["ok"]
    data = design.load("Single")
    assert [x["name"] for x in data["plan"]["parts"]] == ["block", "crankshaft", "rod1", "piston1"]
    assert all(x["status"] == "built" for x in data["plan"]["parts"])
    assert data["assembly"]["joints"] and "all parts built" in design.summary("Single")


def test_summary_is_short_even_for_big_jobs(app):
    parse(plan_machine(project="Big", goal="many parts",
                       parts="\n".join(f"part{i}: a long description of part {i} " * 3 for i in range(60))))
    assert len(design.summary("Big")) <= design.SUMMARY_CHARS
