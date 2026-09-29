"""Documents, joints, turning parts and motion studies (against the fake SolidWorks)."""

from __future__ import annotations

import json

import pytest

from sw_mcp.core import connection, typelib
from sw_mcp.core.errors import Code
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import mechanism as mech
from sw_mcp.sw import modeling as m
from sw_mcp.sw import plan as p
from sw_mcp.tools.documents import manage_documents
from sw_mcp.tools.mechanism import connect_parts, make_motion_study, move_mechanism
from sw_mcp.tools.plan import build_part
from sw_mcp.tools.project import list_project as list_project_tool
from sw_mcp.tools.project import make_assembly
from tests.conftest import parse


# ---------------------------------------------------------------- pure math
def test_rotation_and_compose():
    rot = mech.rotation_about([0, 0, 1], 90, [10, 0, 0])
    moved = mech.apply_transform(rot, [0.011, 0, 0])  # meters in, meters out
    assert [round(v * 1000, 6) for v in moved] == [10, 1, 0]
    twice = mech.compose(rot, rot)
    assert [round(v * 1000, 6) for v in mech.apply_transform(twice, [0.011, 0, 0])] == [9, 0, 0]
    assert mech.apply_transform(mech.IDENTITY, [1, 2, 3]) == [1, 2, 3]


def test_coaxial_detection():
    shaft = mech.CylFace("crank-1", None, [0, 30, 0], [1, 0, 0], 10.0, -60, 60)
    bore = mech.CylFace("block-1", None, [5, 30, 0], [-1, 0, 0], 10.2, -50, 50)
    off_axis = mech.CylFace("block-1", None, [0, 31, 0], [1, 0, 0], 10.0, -50, 50)
    too_loose = mech.CylFace("block-1", None, [0, 30, 0], [1, 0, 0], 12.0, -50, 50)
    elsewhere = mech.CylFace("block-1", None, [200, 30, 0], [1, 0, 0], 10.0, -10, 10)
    assert mech.coaxial(shaft, bore)
    assert not mech.coaxial(shaft, off_axis) and not mech.coaxial(shaft, too_loose)
    assert not mech.coaxial(shaft, elsewhere)  # same axis line but no overlap along it
    joints = mech.find_joints([shaft, bore, too_loose])
    assert len(joints) == 1 and joints[0][1].radius == 10.2


# ---------------------------------------------------------------- against the fake
@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_last_group", {})
    monkeypatch.setattr(p, "_unsaved_failed_builds", [])
    monkeypatch.setenv("SW_MCP_PROJECTS", str(tmp_path / "projects"))
    fake = make_modeling_app()
    connection.use_app_factory(lambda: fake)
    yield fake
    connection.use_app_factory(None)


BLOCK = {"steps": [{"op": "box", "x": [-60, 60], "y": [0, 60], "z": [-30, 30]},
                   {"op": "cylinder", "mode": "cut", "start": [-61, 30, 0], "end": [61, 30, 0], "diameter": 20}]}
CRANK = {"steps": [{"op": "cylinder", "start": [-70, 30, 0], "end": [70, 30, 0], "diameter": 20},
                   {"op": "box", "x": [62, 66], "y": [30, 50], "z": [-4, 4]}]}
HEAD = {"steps": [{"op": "box", "x": [-60, 60], "y": [60, 70], "z": [-30, 30]}]}


def build_engine() -> None:
    for name, plan in (("block", BLOCK), ("crank", CRANK), ("head", HEAD)):
        assert parse(build_part(plan=json.dumps(plan), save_as=f"Mini/{name}"))["ok"]
    assert parse(make_assembly(project="Mini", name="Mini"))["ok"]


def test_saved_parts_close_their_window(app):
    out = parse(build_part(plan=json.dumps(HEAD), save_as="Mini/head"))
    assert out["window"].startswith("closed") and app.closed == ["head.SLDPRT"]
    kept = parse(build_part(plan=json.dumps(HEAD), save_as="Mini/head2", keep_open=True))
    assert "window" not in kept and "head2.SLDPRT" not in app.closed


def test_manage_documents(app):
    parse(build_part(plan=json.dumps(HEAD)))  # Part1, unsaved
    parse(build_part(plan=json.dumps(HEAD), save_as="Mini/a", keep_open=True))  # Part2, saved
    listed = parse(manage_documents(action="list"))
    assert listed["open"] == 2 and listed["unsaved"] == ["Part1"]
    assert parse(manage_documents(action="activate", name="Part1"))["active"]["name"] == "Part1"
    out = parse(manage_documents(action="close", name="all"))
    assert out["closed"] == ["a.SLDPRT"] and out["kept_unsaved"] == ["Part1"] and "discard_unsaved" in out["note"]
    out = parse(manage_documents(action="close", name="Part1", discard_unsaved=True))
    assert out["closed"] == ["Part1"] and out["still_open"] == 0
    assert parse(manage_documents(action="activate", name="Nope"))["error"] == Code.NOT_FOUND


def test_connect_parts_joins_shaft_to_bore_and_fixes_the_rest(app):
    build_engine()
    out = parse(connect_parts(fixed_part="block"))
    assert out["ok"] and len(out["joints"]) == 1
    assert {"block-1", "crank-1"} <= set(out["joints"][0].replace("+", " ").split())
    assert sorted(out["fixed_parts"]) == ["block-1", "head-1"] and out["can_move"] == ["crank-1"]
    asm = app.created[-1]
    assert len(asm.mates) == 1 and asm.mates[0][0] == 1 and set(asm.mates[0][1:]) == {"block-1", "crank-1"}
    assert {c.Name2: c.fixed for c in asm.components} == {"block-1": True, "crank-1": False, "head-1": True}


def test_move_mechanism_turns_the_part(app):
    build_engine()
    parse(connect_parts(fixed_part="block"))
    out = parse(move_mechanism(part="crank", degrees=180, steps=6))
    assert out["ok"] and out["turned"] == "crank-1"
    assert out["axis"]["direction"] in ([1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]) and out["axis"]["point_mm"][1] == 30.0
    crank = next(x for x in out["moved"] if x["part"] == "crank-1")
    assert crank["travel_mm"] > 5  # the arm swung round
    assert "head-1" in out["did_not_move"]
    assert parse(move_mechanism(part="head"))["error"] == Code.NOT_FOUND  # no joint to turn around


def test_motion_study(app, monkeypatch):
    build_engine()
    parse(connect_parts())
    monkeypatch.setattr(typelib, "constants", lambda: {"swFmAEMRotaryMotor": 7, "swMotionStudyTypeAssembly": 1})
    out = parse(make_motion_study(part="crank", rpm=120, seconds=4))
    assert out["ok"] and out["motor_on"] == "crank-1" and out["calculated"]
    study = app.created[-1].motion.study
    assert study.duration == 4.0 and study.StudyType == 1 and study.played


def test_motion_study_failures_are_explained(app, monkeypatch):
    build_engine()
    parse(connect_parts())
    monkeypatch.setattr(typelib, "constants", lambda: {"swFmAEMRotaryMotor": 7})
    asm = app.created[-1]
    asm.motion.study.accept_motor = False
    out = parse(make_motion_study(part="crank"))
    assert out["error"] == Code.SW_ERROR and "by hand" in out["fix"]
    asm.motion = None
    assert parse(make_motion_study(part="crank"))["error"] == Code.UNSUPPORTED


def test_mechanism_tools_need_an_assembly(app):
    parse(build_part(plan=json.dumps(HEAD)))
    assert parse(connect_parts())["error"] == Code.WRONG_DOC_TYPE


def test_close_others_and_new(app):
    parse(build_part(plan=json.dumps(HEAD)))  # Part1, never saved
    parse(build_part(plan=json.dumps(HEAD), save_as="Mini/a", keep_open=True))  # a.SLDPRT, saved
    parse(build_part(plan=json.dumps(HEAD)))  # Part3, never saved and active
    out = parse(manage_documents(action="close", name="new"))
    assert out["closed"] == [] and sorted(out["kept_unsaved"]) == ["Part1", "Part3"]
    out = parse(manage_documents(action="close", name="others", discard_unsaved=True))
    assert sorted(out["closed"]) == ["Part1", "a.SLDPRT"] and "Part3" not in out["closed"]


def test_rebuilding_a_saved_part_releases_the_open_assembly(app):
    build_engine()
    asm = app.created[-1]
    assert asm.path.endswith("Mini.SLDASM")
    out = parse(build_part(plan=json.dumps(HEAD), save_as="Mini/head"))
    assert out["ok"] and "Mini.SLDASM" in out["closed_to_replace"]
    assert parse(list_project_tool(project="Mini"))["parts"] == ["block", "crank", "head"]


def test_list_project_hides_lock_files(app, tmp_path):
    build_engine()
    folder = tmp_path / "projects" / "Mini"
    (folder / "~$block.SLDPRT").write_bytes(b"lock")
    listed = parse(list_project_tool(project="Mini"))
    assert "~$block" not in listed["parts"]


def test_rod_between_the_two_halves_of_a_pin_hole_is_a_joint():
    """Log 2026-09-28 15:56: on real SolidWorks the piston's slot splits its pin hole into two faces
    beside the rod, so the faces sit side by side (0.5 mm gap) instead of overlapping."""
    rod = mech.CylFace("rod1-1", None, [-10.6, 150, 0], [1, 0, 0], 10.9, 0, 21.2)
    pin_half = mech.CylFace("piston1-1", None, [11.1, 150, 0], [1, 0, 0], 10.6, 0, 32)
    far = mech.CylFace("piston1-1", None, [20, 150, 0], [1, 0, 0], 10.6, 0, 32)
    assert mech.coaxial(rod, pin_half) and not mech.coaxial(rod, far)


def test_motion_study_sets_the_motor_like_the_solidworks_example(app, monkeypatch):
    build_engine()
    parse(connect_parts())
    monkeypatch.setattr(typelib, "constants", lambda: {"swFmAEMRotaryMotor": 7, "swMotionStudyTypeAssembly": 1})
    out = parse(make_motion_study(part="crank", rpm=600, seconds=5))
    motor = app.created[-1].motion.study.definition
    assert out["ok"] and motor.rpm == 600 and motor.DirectionReference is not None and motor.Location is not None
