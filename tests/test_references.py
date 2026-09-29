"""Named frames and axes: parts that name the same axis line up; frames spare the model trigonometry."""

from __future__ import annotations

import json
import math

import pytest

from sw_mcp.core import connection
from sw_mcp.core.errors import Code, SwError
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import design
from sw_mcp.sw import modeling as m
from sw_mcp.sw import plan as p
from sw_mcp.sw import references as r
from sw_mcp.tools.mechanism import connect_parts
from sw_mcp.tools.plan import build_part
from sw_mcp.tools.project import make_assembly, plan_machine
from tests.conftest import parse


def test_reference_lines_are_read():
    refs = r.parse("axis input through 0,0,0 along y\naxis back: at (250, 0, -10) along -x; "
                   "axis bank through 0 0 0 along 0,0.7071,0.7071\nframe left_bank origin 0,40,0 turn x 45\n"
                   "- frame plain")
    assert refs.axes["input"].direction == (0.0, 1.0, 0.0) and refs.axes["back"].point == (250.0, 0.0, -10.0)
    assert refs.axes["back"].direction == (-1.0, 0.0, 0.0)
    assert math.isclose(refs.axes["bank"].direction[2], math.sqrt(0.5), rel_tol=1e-3)
    assert (refs.frames["left_bank"].axis, refs.frames["left_bank"].deg) == ("x", 45.0)
    assert refs.frames["plain"].origin == (0.0, 0.0, 0.0) and refs.frames["plain"].axis is None
    assert r.References.from_dict(refs.to_dict()).describe() == refs.describe()
    for bad in ("shaft input along y", "axis input through 0,0,0"):
        with pytest.raises(SwError):
            r.parse(bad)


def test_shapes_on_axes_and_in_frames():
    refs = r.parse("axis input through 10,0,5 along y\naxis back through 0,50,0 along -x\n"
                   "frame bank origin 0,40,0 turn x 45")
    shaft = r.cylinder_on_axis(refs.axis("input"), 0, 200, 30, cut=False)
    assert (shaft.axis, shaft.start, shaft.end, shaft.profile.points) == ("y", 0, 200, [(10, 5)])
    disc = r.revolve_on_axis(refs.axis("back"), [(0, 0), (40, 0), (40, 10), (0, 10)], 360, cut=False)
    assert disc.axis == "x" and disc.box()[0][0] == -10 and disc.box()[1][0] == 0  # measured along -x
    box = r.in_frame(m.box_shape(-20, 20, 0, 100, -20, 20, cut=False), refs.frame("bank"))
    assert box.tilt == ("x", 45.0, (0.0, 40.0, 0.0)) and box.start == 40  # moved up to the origin, then turned
    with pytest.raises(SwError) as info:
        r.in_frame(m.cylinder_shape(0, 0, 0, 0, 10, 10, 5, cut=False), refs.frame("bank"))
    assert "upright" in info.value.fix
    with pytest.raises(SwError) as info:
        refs.axis("nope")
    assert "Known axes: input, back" in info.value.fix


def test_plan_steps_use_named_references():
    plan = p.parse_plan(json.dumps({"references": "axis a through 0,0,0 along z\nframe f origin 5,0,0 turn z 30",
                                    "steps": [{"op": "cylinder", "on_axis": "a", "from": -5, "to": 5, "diameter": 8},
                                              {"op": "box", "frame": "f", "x": [0, 10], "y": [0, 2], "z": [0, 2]},
                                              {"op": "revolve", "on_axis": "a", "profile": [[4, 5], [9, 5], [9, 7], [4, 7]]}]}))
    shapes = p.check_plan(plan)
    assert shapes[0].axis == "z" and (shapes[0].start, shapes[0].end) == (-5, 5)
    assert shapes[1].tilt == ("z", 30.0, (5.0, 0.0, 0.0)) and isinstance(shapes[2], m.Revolve)
    for bad, words in (({"op": "cylinder", "on_axis": "a", "diameter": 8}, '"from" and "to"'),
                       ({"op": "cylinder", "diameter": 8}, '"start" and "end"'),
                       ({"op": "revolve", "profile": [[0, 0], [1, 0], [1, 1]]}, '"axis"')):
        with pytest.raises(SwError) as info:
            p.parse_plan(json.dumps({"steps": [bad]}))
        assert words in info.value.message


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


def test_parts_on_the_same_named_axis_are_joined(app):
    """A small CVT: two shafts and two pulleys, described only with named axes; no coordinates to
    keep consistent by hand, so connect_parts finds every shaft-in-bore joint."""
    out = parse(plan_machine(project="CVT", goal="two pulleys on two shafts 250 mm apart",
                             parts="input_shaft\noutput_shaft\npulley_a\npulley_b",
                             references="axis input through 0,0,0 along y\naxis output through 250,0,0 along y"))
    assert out["ok"] and "axis input: through (0, 0, 0) along y" in out["design"]
    shaft = {"steps": [{"op": "cylinder", "on_axis": "input", "from": 0, "to": 200, "diameter": 30}]}
    pulley = {"steps": [{"op": "revolve", "on_axis": "input", "profile": [[15.25, 80], [75, 80], [75, 110], [15.25, 110]]}]}
    for name, plan in (("input_shaft", shaft), ("pulley_a", pulley)):
        assert parse(build_part(plan=json.dumps(plan), save_as=f"CVT/{name}"))["ok"]
    for name, plan in (("output_shaft", shaft), ("pulley_b", pulley)):
        other = json.loads(json.dumps(plan).replace('"input"', '"output"'))
        assert parse(build_part(plan=json.dumps(other), save_as=f"CVT/{name}"))["ok"]
    assert design.load("CVT")["parts"]["output_shaft"]["min_mm"][0] == 235.0
    assert parse(make_assembly(project="CVT", name="CVT"))["ok"]
    out = parse(connect_parts(fixed_part="input_shaft"))
    assert len(out["joints"]) >= 1 and "pulley_a-1" in out["can_move"]


def test_a_part_in_a_turned_frame_is_built(app):
    parse(plan_machine(project="V", goal="bank test", parts="bank", references="frame left origin 0,0,0 turn x 45"))
    out = parse(build_part(plan=json.dumps({"steps": [
        {"op": "box", "frame": "left", "x": [-20, 20], "y": [40, 120], "z": [-20, 20]},
        {"op": "cylinder", "mode": "cut", "frame": "left", "start": [0, 39, 0], "end": [0, 121, 0], "diameter": 20}]}),
        save_as="V/bank"))
    assert out["ok"] and out["bodies"] == 1
    assert out["min_mm"][2] > 0 and out["max_mm"][1] < 120  # leaned 45 degrees toward +Z (+Y turned about +X)
    assert parse(build_part(plan=json.dumps({"steps": [{"op": "box", "frame": "nope", "x": [0, 1], "y": [0, 1],
                                                        "z": [0, 1]}]}), save_as="V/x"))["error"] == Code.BAD_ARGUMENT
