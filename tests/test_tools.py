from __future__ import annotations

import os

from sw_mcp.core.errors import Code
from sw_mcp.sw import constants as C
from sw_mcp.tools.context import get_selection_context
from sw_mcp.tools.dimensions import set_dimension
from sw_mcp.tools.documents import open_document, save_document
from sw_mcp.tools.session import get_status
from tests.conftest import parse
from sw_mcp.fakes.fake_sw import (
    FakeComponent, FakeDimension, FakeDisplayDimension, FakeDoc, FakeFeature, FakeMate, block_part,
)


# ---------------------------------------------------------------- get_status
def test_status_ready(fake_app):
    doc, _ = block_part()
    fake_app.ActiveDoc = doc
    out = parse(get_status())
    assert out["ok"] and out["version"] == "2025 SP1"
    assert out["active_document"]["name"] == "Block.SLDPRT"
    assert fake_app.Visible is True


def test_status_starts_solidworks_once(no_solidworks):
    first = parse(get_status())
    second = parse(get_status())
    assert first["error"] == second["error"] == Code.SW_STARTING
    assert no_solidworks == [r"C:\SW\SLDWORKS.exe"]  # launched exactly once


def test_status_never_launches_a_second_instance(no_solidworks, monkeypatch):
    from sw_mcp.core import connection

    monkeypatch.setattr(connection, "solidworks_pids", lambda: [4242])
    out = parse(get_status())
    assert out["error"] == Code.SW_STARTING and "4242" in out["message"]
    assert no_solidworks == []


def test_other_tools_do_not_launch(no_solidworks):
    out = parse(get_selection_context())
    assert out["error"] == Code.NO_SOLIDWORKS and "get_status" in out["fix"]
    assert no_solidworks == []


def test_reconnects_after_solidworks_restart(fake_app, monkeypatch):
    from sw_mcp.core import connection
    from sw_mcp.fakes.fake_sw import FakeApp

    assert parse(get_status())["ok"]
    fake_app.alive = False
    new_app = FakeApp(revision="32.0.0")
    monkeypatch.setattr(connection, "_attach_running", lambda: new_app)
    assert parse(get_status())["version"] == "2024 SP0"


# ---------------------------------------------------------------- open_document
def test_open_missing_file(fake_app, tmp_path):
    out = parse(open_document(file_path=str(tmp_path / "nope.SLDPRT")))
    assert out["error"] == Code.FILE_NOT_FOUND


def test_open_wrong_extension(fake_app, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    assert parse(open_document(file_path=str(f)))["error"] == Code.BAD_ARGUMENT


def test_open_ok_and_already_open(fake_app, tmp_path):
    f = tmp_path / "Bracket.SLDPRT"
    f.write_bytes(b"x")
    out = parse(open_document(file_path=f'"{f}"'))  # quotes are tolerated
    assert out["ok"] and out["opened"]["type"] == "part"
    again = parse(open_document(file_path=str(f)))
    assert again["ok"] and "already open" in again["note"]


def test_open_reports_solidworks_reason(fake_app, tmp_path):
    f = tmp_path / "Future.SLDPRT"
    f.write_bytes(b"x")
    fake_app.open_error = 8192
    out = parse(open_document(file_path=str(f)))
    assert out["error"] == Code.OPEN_FAILED and "newer SolidWorks version" in out["message"]


# ---------------------------------------------------------------- get_selection_context
def test_context_empty_selection_hints_user(fake_app):
    fake_app.ActiveDoc, _ = block_part()
    out = parse(get_selection_context())
    assert out["selected_count"] == 0 and "click" in out["hint"]


def test_context_no_document(fake_app):
    assert parse(get_selection_context())["error"] == Code.NO_ACTIVE_DOC


def test_context_face_with_dims(fake_app):
    doc, g = block_part()
    fake_app.ActiveDoc = doc
    doc.SelectionManager.items = [(C.SEL_FACES, g["top"], None)]
    item = parse(get_selection_context())["items"][0]
    assert item["surface"] == "plane" and item["normal"] == [0.0, 1.0, 0.0]
    assert item["area_mm2"] == 800.0 and item["feature"] == "Boss-Extrude1"
    names = [d["name"] for d in item["dims"]]
    assert names == ["D1@Boss-Extrude1", "D1@Sketch1", "D2@Sketch1"]
    assert item["dims"][0] == {"name": "D1@Boss-Extrude1", "value": 10.0, "unit": "mm"}


def test_context_cylinder_edge_and_dimension(fake_app):
    doc, g = block_part()
    fake_app.ActiveDoc = doc
    doc.SelectionManager.items = [
        (C.SEL_FACES, g["hole"], None),
        (C.SEL_EDGES, g["edge"], None),
        (C.SEL_DIMENSIONS, FakeDisplayDimension(g["depth"]), None),
    ]
    items = parse(get_selection_context())["items"]
    assert items[0]["surface"] == "cylinder" and items[0]["diameter_mm"] == 5.0
    assert items[1]["kind"] == "line" and items[1]["length_mm"] == 40.0 and items[1]["features"] == ["Boss-Extrude1"]
    assert items[2] == {"type": "dimension", "name": "D1@Boss-Extrude1", "value": 10.0, "unit": "mm"}


def test_context_mate_in_assembly(fake_app):
    asm = FakeDoc(title="Robot.SLDASM", doc_type=2)
    base, arm = FakeComponent("base-1"), FakeComponent("arm-1")
    dist = FakeDimension("D1", "Distance1", 0.005, doc_title="Robot.SLDASM")
    mate_feat = FakeFeature("Distance1", "MateDistanceDim", [dist], specific=FakeMate(5, [base, arm]))
    asm.SelectionManager.items = [(C.SEL_MATES, mate_feat, None)]
    fake_app.ActiveDoc = asm
    item = parse(get_selection_context())["items"][0]
    assert item["mate_type"] == "distance" and item["components"] == ["base-1", "arm-1"]
    assert item["dims"][0]["value"] == 5.0 and item["dims"][0]["name"].count("@") == 2


def test_context_truncates(fake_app):
    doc, g = block_part()
    fake_app.ActiveDoc = doc
    doc.SelectionManager.items = [(C.SEL_EDGES, g["edge"], None)] * 7
    out = parse(get_selection_context(max_items=2))
    assert len(out["items"]) == 2 and out["selected_count"] == 7 and "truncated" in out


def test_context_survives_one_unreadable_item(fake_app):
    doc, g = block_part()
    fake_app.ActiveDoc = doc

    class Weird:
        @property
        def Name(self):
            raise RuntimeError("boom")

    doc.SelectionManager.items = [(C.SEL_DATUMPLANES, Weird(), None), (C.SEL_EDGES, g["edge"], None)]
    items = parse(get_selection_context())["items"]
    assert items[0]["type"] == "unreadable" and items[1]["type"] == "edge"


# ---------------------------------------------------------------- set_dimension
def test_set_dimension_ok(fake_app):
    doc, g = block_part()
    fake_app.ActiveDoc = doc
    out = parse(set_dimension(dimension_name="D1@Boss-Extrude1", new_value=25))
    assert out == {"ok": True, "dimension": "D1@Boss-Extrude1", "old": 10.0, "new": 25.0, "unit": "mm",
                   "next": "Call save_document to keep the change."}
    assert abs(g["depth"].SystemValue - 0.025) < 1e-12


def test_set_dimension_accepts_full_name(fake_app):
    doc, g = block_part()
    fake_app.ActiveDoc = doc
    assert parse(set_dimension(dimension_name="D1@Boss-Extrude1@Block.Part", new_value=12))["ok"]


def test_set_dimension_rolls_back_on_rebuild_failure(fake_app):
    doc, g = block_part()
    fake_app.ActiveDoc = doc
    out = parse(set_dimension(dimension_name="D1@Boss-Extrude1", new_value=900))
    assert out["error"] == Code.REBUILD_FAILED and "10.0 mm was restored" in out["message"]
    assert abs(g["depth"].SystemValue - 0.010) < 1e-12


def test_set_dimension_rolls_back_on_new_rebuild_errors(fake_app, monkeypatch):
    doc, g = block_part()
    fake_app.ActiveDoc = doc
    original = doc.EditRebuild3

    def rebuild_with_new_problem():
        doc.problems = 1 if g["depth"].SystemValue > 0.02 else 0
        return original()

    monkeypatch.setattr(doc, "EditRebuild3", rebuild_with_new_problem)
    assert parse(set_dimension(dimension_name="D1@Boss-Extrude1", new_value=30))["error"] == Code.REBUILD_FAILED


def test_set_dimension_angle_and_warning(fake_app):
    doc, g = block_part()
    doc.dims["D3@Sketch1"] = FakeDimension("D3", "Sketch1", 0.5235987756, kind=2)
    fake_app.ActiveDoc = doc
    out = parse(set_dimension(dimension_name="D3@Sketch1", new_value=45))
    assert out["unit"] == "deg" and out["old"] == 30.0 and out["new"] == 45.0
    tiny = parse(set_dimension(dimension_name="D1@Sketch1", new_value=0.04))
    assert "warning" in tiny


def test_set_dimension_errors(fake_app):
    doc, g = block_part()
    doc.dims["RD1@Sketch1"] = FakeDimension("RD1", "Sketch1", 0.01, read_only=True)
    fake_app.ActiveDoc = doc
    assert parse(set_dimension(dimension_name="D9@Nope", new_value=5))["error"] == Code.NOT_FOUND
    assert parse(set_dimension(dimension_name="RD1@Sketch1", new_value=5))["error"] == Code.READ_ONLY
    fake_app.ActiveDoc = FakeDoc(title="Sheet.SLDDRW", doc_type=3)
    assert parse(set_dimension(dimension_name="D1@Sketch1", new_value=5))["error"] == Code.WRONG_DOC_TYPE


# ---------------------------------------------------------------- save_document
def test_save_in_place(fake_app, tmp_path):
    doc, _ = block_part()
    doc.dirty = True
    fake_app.ActiveDoc = doc
    assert parse(save_document())["saved"] == doc.path
    assert doc.dirty is False


def test_save_new_doc_needs_path(fake_app):
    fake_app.ActiveDoc = FakeDoc(path="")
    assert parse(save_document())["error"] == Code.BAD_ARGUMENT


def test_export_and_overwrite_protection(fake_app, tmp_path):
    doc, _ = block_part()
    fake_app.ActiveDoc = doc
    target = tmp_path / "block.step"
    out = parse(save_document(save_as_path=str(target)))
    assert out["exported"] == str(target) and os.path.isfile(target)
    assert doc.path == "C:\\Parts\\Block.SLDPRT"  # an export does not rename the document
    assert parse(save_document(save_as_path=str(target)))["error"] == Code.FILE_EXISTS
    assert parse(save_document(save_as_path=str(target), overwrite=True))["ok"]


def test_save_as_validation(fake_app, tmp_path):
    fake_app.ActiveDoc, _ = block_part()
    assert parse(save_document(save_as_path=str(tmp_path / "x.docx")))["error"] == Code.BAD_ARGUMENT
    assert parse(save_document(save_as_path=str(tmp_path / "x.SLDASM")))["error"] == Code.BAD_ARGUMENT
    assert parse(save_document(save_as_path=str(tmp_path / "missing" / "x.step")))["error"] == Code.FILE_NOT_FOUND


def test_save_reports_solidworks_reason(fake_app, tmp_path):
    doc, _ = block_part()
    doc.save_error = 16
    fake_app.ActiveDoc = doc
    out = parse(save_document(save_as_path=str(tmp_path / "copy.SLDPRT")))
    assert out["error"] == Code.SAVE_FAILED and "locked" in out["message"]
