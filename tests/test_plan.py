from __future__ import annotations

import json

import pytest

from sw_mcp.core.errors import Code, SwError
from sw_mcp.sw import modeling as m
from sw_mcp.sw import plan as p
from sw_mcp.tools.plan import build_part
from tests.conftest import parse
from sw_mcp.fakes.fake_modeler import FakePart

PLATE = {
    "steps": [
        {"op": "box", "x": [-30, 30], "y": [0, 10], "z": [-20, 20]},
        {"op": "fillet", "size": 5, "edges": "vertical"},
        {"op": "cylinder", "mode": "cut", "start": [-22, -1, -12], "end": [-22, 11, -12], "diameter": 6},
        {"op": "repeat", "copies": 1, "step": [44, 0, 0]},
        {"op": "repeat", "copies": 1, "step": [0, 0, 24]},
    ],
    "expect": {"size": [60, 10, 40]},
}


# ---------------------------------------------------------------- parsing
def test_lenient_json():
    text = """Here is the plan:
```json
{
  // base plate
  "steps": [{"op": "box", "x": [0, 1], "y": [0, 1], "z": [0, 1],},],
}
```"""
    plan = p.parse_plan(text)
    assert plan.steps[0].op == "box"
    assert p.parse_plan(json.dumps(PLATE["steps"])).expect is None  # a bare list of steps


def test_points_accept_text():
    plan = p.parse_plan('{"steps":[{"op":"prism","axis":"z","points":"0,0; 5,0; 0,5","start":0,"end":2}]}')
    assert plan.steps[0].points == [(0, 0), (5, 0), (0, 5)]


@pytest.mark.parametrize("steps, expected", [
    ([{"op": "hole", "x": 1}], "step 1 (hole): unknown op"),
    ([{"x": [0, 1]}], 'every step needs "op"'),
    ([{"op": "box", "x": [0, 1], "y": [0, 1]}], "step 1 (box), z: Field required"),
    ([{"op": "box", "x": [0, 1], "y": [0, 1], "z": [0, 1]},
      {"op": "cylinder", "start": [0, 0, 0], "end": [0, 5, 0], "diameter": -2}],
     "step 2 (cylinder), diameter"),
    ([{"op": "box", "x": [0, 1], "y": [0, 1], "z": [0, 1], "colour": "red"}], "not allowed"),
])
def test_schema_errors_name_the_step(steps, expected):
    with pytest.raises(SwError) as info:
        p.parse_plan(json.dumps({"steps": steps}))
    assert info.value.code == Code.BAD_ARGUMENT and expected in info.value.message
    assert "Nothing was built" in info.value.fix


def test_not_json():
    with pytest.raises(SwError) as info:
        p.parse_plan("make a box please")
    assert "not valid JSON" in info.value.message


@pytest.mark.parametrize("steps, expected", [
    ([{"op": "box", "x": [0, 1], "y": [0, 1], "z": [0, 1]},
      {"op": "cylinder", "start": [0, 0, 0], "end": [3, 5, 0], "diameter": 2}], "Step 2 (cylinder)"),
    ([{"op": "repeat", "copies": 2, "step": [1, 0, 0]}, {"op": "box", "x": [0, 1], "y": [0, 1], "z": [0, 1]}],
     "Step 1 (repeat) has nothing to repeat"),
    ([{"op": "box", "x": [0, 1], "y": [0, 1], "z": [0, 1]}, {"op": "repeat", "copies": 2, "step": [0, 0, 0]}],
     "zero step"),
    ([{"op": "fillet", "size": 1, "edges": "all"}], "no box, cylinder, prism or revolve"),
])
def test_dry_run_catches_geometry_mistakes(steps, expected):
    with pytest.raises(SwError) as info:
        p.check_plan(p.parse_plan(json.dumps({"steps": steps})))
    assert expected in info.value.message


# ---------------------------------------------------------------- execution against the fake part
@pytest.fixture
def app(fake_app, monkeypatch):
    monkeypatch.setattr(m, "_last_group", {})
    monkeypatch.setattr(p, "_unsaved_failed_builds", [])
    counter = {"n": 0}
    closed: list[str] = []

    def new_document(template, a, b, c):
        counter["n"] += 1
        doc = FakePart(f"Part{counter['n']}")
        fake_app.ActiveDoc = doc
        return doc

    fake_app.GetUserPreferenceStringValue = lambda i: "C:\\t\\part.prtdot"
    fake_app.NewDocument = new_document
    fake_app.CloseDoc = lambda title: closed.append(title)
    fake_app.closed = closed
    return fake_app


def test_build_plate_in_one_call(app):
    out = parse(build_part(plan=json.dumps(PLATE)))
    assert out["ok"] and out["steps_built"] == 5 and out["features"] == 6  # box, fillet, 4 holes
    assert out["size_mm"] == [60.0, 10.0, 40.0] and out["check"] == "matches the plan"
    holes = sorted(f.Name for f in app.ActiveDoc.features if f.Name.startswith("Hole"))
    assert holes == ["Hole1", "Hole2", "Hole3", "Hole4"]


def test_expect_mismatch_is_reported(app):
    plan = dict(PLATE, expect={"size": [60, 12, 40]})
    out = parse(build_part(plan=json.dumps(plan)))
    assert out["ok"] and out["check"].startswith("MISMATCH") and "[60.0, 12.0, 40.0]" in out["check"]


def test_failure_names_step_and_progress_then_cleans_up(app):
    bad = {"steps": [
        {"op": "box", "x": [-30, 30], "y": [0, 10], "z": [-20, 20]},
        {"op": "cylinder", "mode": "cut", "start": [0, -1, 0], "end": [0, 11, 0], "diameter": 5},
        {"op": "box", "mode": "cut", "x": [100, 110], "y": [0, 5], "z": [0, 5]},
    ]}
    out = parse(build_part(plan=json.dumps(bad)))
    assert out["error"] == Code.SW_ERROR
    assert "Step 3 (box) failed" in out["message"] and "Steps 1-2 were built (Box1, Hole1)" in out["message"]
    assert "whole corrected plan" in out["fix"]
    good = parse(build_part(plan=json.dumps(PLATE)))
    assert good["ok"] and app.closed == ["Part1"]  # the failed attempt was closed, not saved


def test_invalid_plan_never_touches_solidworks(app):
    out = parse(build_part(plan='{"steps":[{"op":"cylinder","start":[0,0,0],"end":[1,1,0],"diameter":3}]}'))
    assert out["error"] == Code.BAD_ARGUMENT and "Step 1 (cylinder)" in out["message"]
    assert app.ActiveDoc is None  # no part was even created


def test_add_to_open_part(app):
    parse(build_part(plan=json.dumps({"steps": PLATE["steps"][:1]})))
    doc = app.ActiveDoc
    out = parse(build_part(plan=json.dumps({"steps": [
        {"op": "cylinder", "start": [0, 10, 0], "end": [0, 20, 0], "diameter": 10}]}), start_new_part=False))
    assert out["ok"] and app.ActiveDoc is doc and out["size_mm"] == [60.0, 20.0, 40.0]
