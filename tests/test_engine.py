"""make_engine: generated geometry, exact motion, and a full build against the fake SolidWorks."""

from __future__ import annotations

import json
import math

import pytest

from sw_mcp.core import connection, typelib
from sw_mcp.core.errors import Code
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import engine as e
from sw_mcp.sw import mechanism as mech
from sw_mcp.sw import modeling as m
from sw_mcp.sw import plan as p
from sw_mcp.tools.mechanism import make_engine, make_motion_study, move_mechanism
from tests.conftest import parse


def _mm(data, point):
    """Apply a row-vector ArrayData (translation in meters) to a point in mm."""
    return [v for v in mech.apply_transform([*data[0:9], *[t * 1000 for t in data[9:12]], *data[12:]], point)]


@pytest.mark.parametrize("layout,n", [("v", 4), ("inline", 4), ("boxer", 2), ("v", 8), ("inline", 1), ("v", 2)])
def test_every_generated_plan_is_valid(layout, n):
    eng = e.design(layout, n)
    for plan in eng.parts.values():
        p.check_plan(p.Plan.model_validate(plan))
    assert {"block", "crankshaft", "rod1", "piston1"} <= set(eng.parts)


def test_bad_engine_numbers_are_explained():
    for kwargs, words in (({"layout": "w"}, "layout"), ({"layout": "v", "cylinders": 3}, "even"),
                          ({"stroke": 200, "bore": 80}, "stroke"), ({"cylinders": 12}, "1 to 8")):
        with pytest.raises(Exception) as info:
            e.design(**kwargs)
        assert words in info.value.message and info.value.code == Code.BAD_ARGUMENT


@pytest.mark.parametrize("layout,n", [("v", 4), ("inline", 4), ("boxer", 2)])
def test_motion_keeps_every_joint_together(layout, n):
    eng = e.design(layout, n, bore=80, stroke=70)
    spec = eng.motion()
    r = eng.crank_radius
    for deg in (0, 37, 90, 180, 263, 360):
        pose = e.poses(spec, deg)
        for c in eng.cylinders:
            pin0 = [c.x, *e._yz(r, 0, c.phi, True)]
            wrist0 = [c.x, *e._yz(c.pin_distance(r, eng.rod_length), 0, c.alpha, True)]
            crank_pin = _mm(pose["crankshaft"], pin0)
            big_end = _mm(pose[f"rod{c.index}"], pin0)
            small_end = _mm(pose[f"rod{c.index}"], wrist0)
            wrist = _mm(pose[f"piston{c.index}"], wrist0)
            assert math.dist(crank_pin, big_end) < 1e-6, (deg, c)
            assert math.dist(small_end, wrist) < 1e-6, (deg, c)
            u = [0.0, *e._yz(1, 0, c.alpha, True)]  # the piston stays on its cylinder axis
            off = [wrist[k] - c.x * (k == 0) for k in range(3)]
            along = sum(off[k] * u[k] for k in range(3))
            assert math.dist(off, [along * v for v in u]) < 1e-6
            rod_len = math.dist(big_end, small_end)
            assert math.isclose(rod_len, eng.rod_length, abs_tol=1e-6)


def test_piston_travel_is_the_stroke():
    eng = e.design("v", 4, stroke=70)
    c = eng.cylinders[0]
    s = [c.pin_distance(eng.crank_radius, eng.rod_length, a) for a in range(0, 360, 5)]
    assert math.isclose(max(s) - min(s), 70, abs_tol=1e-6)


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


def test_make_v4_engine_builds_connects_and_turns(app, tmp_path, monkeypatch):
    monkeypatch.setattr(typelib, "constants", lambda: {"swFmAEMRotaryMotor": 7, "swMotionStudyTypeAssembly": 1})
    out = parse(make_engine(project="V4 test", layout="v", cylinders=4, bank_angle=90, bore=80, stroke=70))
    assert out["ok"], out
    assert out["engine"].startswith("V4") and out["displacement_cc"] == pytest.approx(1407.4, abs=0.1)
    assert set(out["parts"]) == {"block", "crankshaft", "head_a", "head_b",
                                 *(f"rod{i}" for i in range(1, 5)), *(f"piston{i}" for i in range(1, 5))}
    assert out["joints"] == 13  # crank-block, and per cylinder: rod-crank pin, rod-piston pin, piston-bore
    assert sorted(out["moving_parts"]) == sorted(["crankshaft-1", *(f"rod{i}-1" for i in range(1, 5)),
                                                  *(f"piston{i}-1" for i in range(1, 5))])
    assert "warnings" not in out, out.get("warnings")
    folder = tmp_path / "projects" / "V4 test"
    motion = json.loads((folder / "V4 test.motion.json").read_text(encoding="utf-8"))
    assert motion["crank"] == "crankshaft" and len(motion["cylinders"]) == 4

    moved = parse(move_mechanism(part="crankshaft", degrees=360, steps=36))
    assert moved["ok"] and "exact engine motion" in moved["motion"]
    travel = {mv["part"]: mv["travel_mm"] for mv in moved["moved"]}
    for i in range(1, 5):
        assert travel[f"piston{i}-1"] == pytest.approx(70, abs=0.2)
    assert {"block-1", "head_a-1", "head_b-1"} <= set(moved["did_not_move"])

    study = parse(make_motion_study(part="crankshaft", rpm=600, seconds=5))
    assert study["ok"] and study["motor_on"] == "crankshaft-1"


def test_make_engine_refuses_bad_numbers_before_building(app):
    out = parse(make_engine(project="Bad", layout="v", cylinders=3))
    assert out["error"] == Code.BAD_ARGUMENT and not app.created
