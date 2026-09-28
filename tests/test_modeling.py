from __future__ import annotations

import math

import pytest

from sw_mcp.core.errors import Code, SwError
from sw_mcp.sw import modeling as m
from sw_mcp.tools.modeling import (
    finish_edges, get_model_summary, make_box, make_cylinder, make_prism, new_part, undo_last_feature,
)
from tests.conftest import parse
from tests.fakes.fake_modeler import FakePart


# ---------------------------------------------------------------- pure geometry
def test_box_shape_sorts_and_validates():
    s = m.box_shape(30, -30, 10, 0, 20, -20, cut=False)
    assert s.axis == "y" and (s.start, s.end) == (0, 10)
    assert s.world_box() == ([-30, 0, -20], [30, 10, 20])
    assert math.isclose(s.volume(), 60 * 40 * 10)
    with pytest.raises(SwError):
        m.box_shape(0, 0, 0, 10, 0, 10, cut=False)


def test_cylinder_shape_axis_and_errors():
    s = m.cylinder_shape(20, 11, -10, 20, -1, -10, 6, cut=True)
    assert s.axis == "y" and (s.start, s.end) == (-1, 11) and s.label == "Hole"
    assert s.profile.points == [(20, -10)] and s.profile.radius == 3
    with pytest.raises(SwError) as info:
        m.cylinder_shape(0, 0, 0, 10, 10, 0, 5, cut=False)
    assert "exactly one coordinate" in info.value.message


def test_parse_points_is_forgiving():
    assert m.parse_points("0,0; 40,0; 40,10; 0,10; 0,0") == [(0, 0), (40, 0), (40, 10), (0, 10)]
    assert m.parse_points("(0 0) (5 0) (0 5)") == [(0, 0), (5, 0), (0, 5)]
    with pytest.raises(SwError):
        m.parse_points("0,0; 1")
    with pytest.raises(SwError):
        m.prism_shape("z", "0,0; 5,0; 10,0", 0, 5, False)  # no area


def test_profile_area_l_shape():
    s = m.prism_shape("z", "0,0; 50,0; 50,5; 5,5; 5,40; 0,40", -15, 15, False)
    assert math.isclose(s.profile.area(), 50 * 5 + 5 * 35)


def test_placement_check():
    s = m.box_shape(-10, 10, 10, 20, -5, 5, cut=False)
    good = [([-10, 10, -5], [10, 20, 5])]
    backwards = [([-10, 0, -5], [10, 10, 5]), ([-10, -1, -5], [10, 0, 5])]
    merged = [([-10, 10, -5], [10, 20, 5]), ([-30, 0, -20], [30, 20, 20])]  # one merged big face
    assert m.placement_ok(s, good)
    assert not m.placement_ok(s, backwards)
    assert m.placement_ok(s, merged)
    assert not m.placement_ok(s, [])


def test_edge_filters():
    lo, hi = [-30, 0, -20], [30, 10, 20]
    vertical = m.EdgeInfo([30, 0, 20], [30, 10, 20], [30, 5, 20], True, False)
    top_x = m.EdgeInfo([-30, 10, 20], [30, 10, 20], [0, 10, 20], True, False)
    rim = m.EdgeInfo([3, 10, 0], [3, 10, 0], [-3, 10, 0], False, True)
    assert m.edge_matches(vertical, "vertical", lo, hi) and not m.edge_matches(top_x, "vertical", lo, hi)
    assert m.edge_matches(top_x, "top", lo, hi) and m.edge_matches(rim, "top", lo, hi)
    assert m.edge_matches(top_x, "parallel_x", lo, hi) and not m.edge_matches(rim, "parallel_x", lo, hi)
    assert m.edge_matches(rim, "circular", lo, hi) and not m.edge_matches(vertical, "bottom", lo, hi)


# ---------------------------------------------------------------- tools against the fake part
@pytest.fixture
def part(fake_app, monkeypatch):
    doc = FakePart()
    fake_app.ActiveDoc = doc
    return doc


def plate(**overrides):
    args = dict(mode="add", x_min_mm=-30, x_max_mm=30, y_min_mm=0, y_max_mm=10, z_min_mm=-20, z_max_mm=20)
    args.update(overrides)
    return parse(make_box(**args))


def test_new_part(fake_app):
    fake_app.GetUserPreferenceStringValue = lambda i: "C:\\t\\part.prtdot"
    fake_app.NewDocument = lambda t, a, b, c: FakePart("Part7")
    out = parse(new_part())
    assert out["ok"] and out["created"] == "Part7"


def test_box_builds_first_try(part):
    out = plate()
    assert out["ok"] and out["feature"] == "Box1"
    assert out["size_mm"] == [60.0, 10.0, 40.0] and out["min_mm"] == [-30.0, 0.0, -20.0]
    assert out["volume_mm3"] == 24000.0 and out["volume_change_mm3"] == 24000.0
    assert part.sketch_count == 1  # no retries needed


def test_wrong_direction_convention_is_corrected(part):
    part.reverse_convention = True
    out = plate(y_min_mm=5, y_max_mm=15)
    assert out["ok"] and out["min_mm"][1] == 5.0 and out["max_mm"][1] == 15.0
    assert part.sketch_count > 1  # it retried, and the failed attempt was deleted
    assert [f.Name for f in part.features if f.GetTypeName2() == "ICE"] == ["Box1"]


def test_mirrored_sketch_axis_is_corrected(part):
    plate()
    part.mirror_second_axis = True
    out = parse(make_cylinder(mode="cut", start_x_mm=20, start_y_mm=-1, start_z_mm=-10,
                              end_x_mm=20, end_y_mm=11, end_z_mm=-10, diameter_mm=6))
    assert out["ok"] and out["feature"] == "Hole1"
    hole = part.FeatureByName("Hole1")
    lo, hi = hole.box
    assert math.isclose((lo[2] + hi[2]) / 2, -10) and math.isclose((lo[0] + hi[0]) / 2, 20)
    assert math.isclose(out["volume_change_mm3"], -round(math.pi * 9 * 12, 1), abs_tol=0.2)


def test_cut_outside_part_reports_clearly(part):
    plate()
    out = parse(make_box(mode="cut", x_min_mm=100, x_max_mm=110, y_min_mm=0, y_max_mm=10,
                         z_min_mm=0, z_max_mm=10))
    assert out["error"] == Code.SW_ERROR and "overlaps" in out["fix"]
    assert [f.GetTypeName2() for f in part.features].count("ProfileFeature") == 0  # attempts cleaned up


def test_prism_along_each_axis(part):
    out = parse(make_prism(mode="add", axis="z", points_mm="0,0; 50,0; 50,5; 5,5; 5,40; 0,40",
                           start_mm=-15, end_mm=15))
    assert out["ok"] and out["min_mm"] == [0.0, 0.0, -15.0] and out["max_mm"] == [50.0, 40.0, 15.0]
    out = parse(make_prism(mode="add", axis="x", points_mm="0,0; 10,0; 10,20; 0,20", start_mm=50, end_mm=60))
    assert out["ok"] and out["max_mm"][0] == 60.0


def test_cylinder_needs_part(fake_app):
    from tests.fakes.fake_sw import FakeDoc

    fake_app.ActiveDoc = FakeDoc(title="A.SLDASM", doc_type=2)
    out = parse(make_cylinder(mode="add", start_x_mm=0, start_y_mm=0, start_z_mm=0,
                              end_x_mm=0, end_y_mm=10, end_z_mm=0, diameter_mm=5))
    assert out["error"] == Code.WRONG_DOC_TYPE


def test_bad_arguments_never_touch_solidworks(part):
    out = parse(make_cylinder(mode="add", start_x_mm=0, start_y_mm=0, start_z_mm=0,
                              end_x_mm=5, end_y_mm=10, end_z_mm=0, diameter_mm=5))
    assert out["error"] == Code.BAD_ARGUMENT and part.sketch_count == 0


def test_finish_edges_vertical(part):
    plate()
    out = parse(finish_edges(kind="fillet", size_mm=3, edges="vertical"))
    assert out["ok"] and out["edges"] == 4 and out["feature"] == "Fillet1"
    assert all(abs(p[1] - 0.005) < 1e-9 for _, p in part.fillet_edges)  # picked at edge midpoints


def test_finish_edges_too_big(part):
    plate()
    part.max_fillet_m = 0.002
    out = parse(finish_edges(kind="fillet", size_mm=3, edges="top"))
    assert out["error"] == Code.SW_ERROR and "smaller" in out["fix"]


def test_undo_and_summary(part):
    plate()
    parse(make_cylinder(mode="add", start_x_mm=0, start_y_mm=10, start_z_mm=0,
                        end_x_mm=0, end_y_mm=20, end_z_mm=0, diameter_mm=10))
    summary = parse(get_model_summary())
    assert [f["name"] for f in summary["features"]] == ["Box1", "Cylinder1"]
    assert summary["size_mm"] == [60.0, 20.0, 40.0]
    out = parse(undo_last_feature())
    assert out["deleted"] == "Cylinder1" and out["size_mm"] == [60.0, 10.0, 40.0]
    assert [f["name"] for f in parse(get_model_summary())["features"]] == ["Box1"]


def test_undo_with_nothing_built(part):
    assert parse(undo_last_feature())["error"] == Code.NOT_FOUND
