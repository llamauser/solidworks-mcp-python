"""Tilted shapes, revolves, projects and assemblies (against the fake SolidWorks)."""

from __future__ import annotations

import json
import math

import pytest

from sw_mcp.core import connection
from sw_mcp.core.errors import Code, SwError
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import modeling as m
from sw_mcp.sw import plan as p
from sw_mcp.sw import project as pr
from sw_mcp.tools.plan import build_part
from sw_mcp.tools.project import list_project, make_assembly
from tests.conftest import parse


# ---------------------------------------------------------------- pure geometry
def test_rotate_point_right_hand():
    assert [round(v, 9) for v in m.rotate_point([10, 0, 0], "z", 90, (0, 0, 0))] == [0, 10, 0]
    assert [round(v, 9) for v in m.rotate_point([0, 10, 0], "x", 90, (0, 0, 0))] == [0, 0, 10]
    assert [round(v, 9) for v in m.rotate_point([0, 0, 10], "y", 90, (0, 0, 0))] == [10, 0, 0]
    assert [round(v, 9) for v in m.rotate_point([11, 0, 0], "z", 180, (10, 0, 0))] == [9, 0, 0]


def test_tilted_box_of_a_bore():
    bore = m.cylinder_shape(0, 0, 0, 0, 100, 0, 20, cut=True)
    bore.tilt = ("z", 45, (0, 0, 0))
    lo, hi = m.tilted_box(bore)
    # a 100 mm bore leaning 45 degrees toward -X: its top center is at (-70.7, 70.7)
    assert math.isclose(lo[0], -70.71 - 7.07, abs_tol=0.3) and math.isclose(hi[1], 70.71 + 7.07, abs_tol=0.3)
    assert math.isclose(lo[2], -10, abs_tol=0.05) and math.isclose(hi[2], 10, abs_tol=0.05)


def test_revolve_spec_validation():
    spec = m.revolve_spec("y", (0, 0, 0), [(0, 0), (20, 0), (20, 10), (0, 10)], 360, cut=False)
    assert spec.sketch_plane() == ("z", 0) and spec.box() == ([-20, 0, -20], [20, 10, 20])
    with pytest.raises(SwError):
        m.revolve_spec("y", (0, 0, 0), [(-5, 0), (20, 0), (20, 10)], 360, cut=False)  # crosses the axis
    with pytest.raises(SwError) as info:
        m.revolve_spec("y", (5, 0, 5), [(0, 0), (20, 0), (20, 10)], 360, cut=False)  # axis off the planes
    assert "center x=0 or z=0" in info.value.fix
    assert m.revolve_spec("x", (0, 30, 0), [(0, 0), (8, 0), (8, 50), (0, 50)], 360, cut=False).sketch_plane() == ("z", 1)


def test_plan_schema_rotate_and_revolve():
    plan = p.parse_plan(json.dumps({"steps": [
        {"op": "box", "x": [-40, 40], "y": [0, 60], "z": [-30, 30]},
        {"op": "cylinder", "mode": "cut", "start": [0, 0, 0], "end": [0, 80, 0], "diameter": 30,
         "rotate": {"axis": "z", "deg": 30}},
        {"op": "revolve", "axis": "y", "profile": "0,60; 10,60; 10,70; 0,70"},
    ]}))
    shapes = p.check_plan(plan)
    assert shapes[1].tilt == ("z", 30.0, (0.0, 0.0, 0.0)) and isinstance(shapes[2], m.Revolve)
    with pytest.raises(SwError) as info:
        p.check_plan(p.parse_plan(json.dumps({"steps": [
            {"op": "cylinder", "start": [10, 0, 0], "end": [10, 5, 0], "diameter": 4, "rotate": {"axis": "x", "deg": 10}},
            {"op": "repeat_around", "copies": 3, "angle_step": 90}]})))
    assert "tilted" in info.value.message


def test_translated_moves_the_tilt_pivot():
    s = m.cylinder_shape(0, 0, 0, 0, 10, 0, 5, cut=False)
    s.tilt = ("z", 45, (0, 0, 0))
    moved = m.translated(s, 0, 0, 30)
    assert moved.tilt == ("z", 45, (0, 0, 30)) and moved.profile.points == [(0, 30)]


# ---------------------------------------------------------------- building against the fake
@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_last_group", {})
    monkeypatch.setattr(p, "_unsaved_failed_builds", [])
    monkeypatch.setenv("SW_MCP_PROJECTS", str(tmp_path / "projects"))
    fake = make_modeling_app()
    connection.use_app_factory(lambda: fake)
    yield fake
    connection.use_app_factory(None)


BLOCK = {"op": "box", "x": [-60, 60], "y": [0, 50], "z": [-30, 30]}


def bore(deg: float, x: float = 0.0) -> dict:
    return {"op": "cylinder", "mode": "cut", "start": [x, 20, 0], "end": [x, 120, 0], "diameter": 30,
            "rotate": {"axis": "z", "deg": deg, "about": [x, 20, 0]}}


def test_tilted_bore_is_cut_into_the_block(app):
    out = parse(build_part(plan=json.dumps({"steps": [BLOCK, bore(45)]})))
    block = 120 * 50 * 60
    assert out["ok"] and out["bodies"] == 1 and out["volume_mm3"] < block - 1000  # the bore removed material
    part = app.created[-1]
    assert not part.extra_bodies  # the tool body was consumed by the combine


def test_tilt_sign_disagreement_is_corrected(app):
    parse(build_part(plan=json.dumps({"steps": [BLOCK]})))
    part = app.created[-1]
    part.rotation_sign = -1.0
    out = parse(build_part(plan=json.dumps({"steps": [bore(-45)]}), start_new_part=False))
    assert out["ok"] and not part.extra_bodies
    moves = [f for f in part.features if f.GetTypeName2() == "MoveCopyBody"]
    assert len(moves) == 1  # the wrong-way attempt was deleted


def test_tilted_first_shape_needs_no_combine(app):
    out = parse(build_part(plan=json.dumps({"steps": [
        {"op": "box", "x": [-5, 5], "y": [0, 40], "z": [-5, 5], "rotate": {"axis": "x", "deg": 20}}]})))
    assert out["ok"] and out["bodies"] == 1


def test_tilted_row_of_bores(app):
    out = parse(build_part(plan=json.dumps({"steps": [
        BLOCK, bore(45, x=-30), {"op": "repeat", "copies": 1, "step": [60, 0, 0]}]})))
    assert out["ok"] and out["features"] == 3 and not app.created[-1].extra_bodies


def test_revolve_disc_volume(app):
    out = parse(build_part(plan=json.dumps({"steps": [
        {"op": "revolve", "axis": "y", "profile": [[0, 0], [20, 0], [20, 10], [0, 10]]}]})))
    assert out["ok"] and math.isclose(out["volume_mm3"], math.pi * 400 * 10, rel_tol=1e-3)
    assert out["size_mm"] == [40.0, 10.0, 40.0]


def test_revolve_cut_groove_on_a_shaft(app):
    out = parse(build_part(plan=json.dumps({"steps": [
        {"op": "cylinder", "start": [0, 0, 0], "end": [0, 60, 0], "diameter": 20},
        {"op": "revolve", "mode": "cut", "axis": "y", "profile": [[8, 25], [11, 25], [11, 30], [8, 30]]}]})))
    shaft, ring = math.pi * 10**2 * 60, math.pi * (10**2 - 8**2) * 5  # the groove only removes what exists
    assert out["ok"] and math.isclose(out["volume_mm3"], shaft - ring, rel_tol=0.01) or         math.isclose(out["volume_mm3"], shaft - math.pi * (11**2 - 8**2) * 5, rel_tol=0.01)


# ---------------------------------------------------------------- projects and assemblies
def test_parts_are_saved_into_a_project_and_assembled(app, tmp_path):
    base = parse(build_part(plan=json.dumps({"steps": [BLOCK]}), save_as="Test engine/block"))
    pin = parse(build_part(plan=json.dumps({"steps": [
        {"op": "cylinder", "start": [0, 50, 0], "end": [0, 90, 0], "diameter": 10}]}), save_as="Test engine/pin"))
    assert base["saved_as"].endswith("Test engine\\block.SLDPRT") or base["saved_as"].endswith("Test engine/block.SLDPRT")
    listed = parse(list_project(project="Test engine"))
    assert listed["parts"] == ["block", "pin"] and listed["assemblies"] == []
    assert parse(list_project())["projects"] == ["Test engine"]
    asm = parse(make_assembly(project="Test engine", parts="all", name="Test engine"))
    assert asm["ok"] and asm["components"] == ["block", "pin"] and "warnings" not in asm
    assert asm["size_mm"] == [120.0, 90.0, 60.0]
    assert parse(list_project(project="Test engine"))["assemblies"] == ["Test engine"]
    assert pin["ok"]


def test_assembly_reports_misplaced_components(app):
    parse(build_part(plan=json.dumps({"steps": [BLOCK]}), save_as="P/block"))
    app.component_offset = (5.0, 0.0, 0.0)  # this SolidWorks places components off their origin
    out = parse(make_assembly(project="P"))
    assert out["ok"] and "off its design position" in out["warnings"][0]


def test_project_errors(app):
    assert parse(make_assembly(project="Nope"))["error"] == Code.NOT_FOUND
    parse(build_part(plan=json.dumps({"steps": [BLOCK]}), save_as="P/block"))
    out = parse(make_assembly(project="P", parts="block, crank"))
    assert out["error"] == Code.NOT_FOUND and "crank" in out["message"] and "block" in out["fix"]
    bad = parse(build_part(plan=json.dumps({"steps": [BLOCK]}), save_as="a/b/c"))
    assert bad["error"] == Code.BAD_ARGUMENT and len(app.created) == 1  # rejected before building


def test_split_name():
    assert pr.split_name("V4 engine/crankshaft") == ("V4 engine", "crankshaft")
    assert pr.split_name("bracket.SLDPRT") == ("Unsorted", "bracket")
    assert pr.split_name(r"V4\piston") == ("V4", "piston")
