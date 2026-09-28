"""End-to-end check on a PC with SolidWorks. Run from the project folder:

    .venv\\Scripts\\python scripts\\smoke_test.py
    .venv\\Scripts\\python scripts\\smoke_test.py --part "C:\\Parts\\any.SLDPRT"   (uses a COPY)

It starts the real MCP server over stdio (exactly like OpenCode does), builds a small
test block (40 x 20 x 10 mm) unless --part is given, then exercises all 5 tools.
Results are printed and written to smoke_test_report.txt. Paste that file back to the
developer. Your own files are never modified: everything happens in a temp folder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import shutil
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pythoncom  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402

from sw_mcp import __version__, config  # noqa: E402
from sw_mcp.core import connection  # noqa: E402
from sw_mcp.core.com_utils import call, call_with_out_ints, null_dispatch, try_call  # noqa: E402

REPORT = os.path.join(ROOT, "smoke_test_report.txt")
results: list[tuple[str, str, str]] = []


def record(step: str, status: str, detail: str = "") -> None:
    results.append((step, status, detail))
    print(f"[{status:4}] {step}" + (f"\n        {detail}" if detail else ""), flush=True)


def short(obj, n: int = 600) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= n else text[: n - 3] + "..."


async def tool(client: Client, name: str, **args) -> dict:
    res = await client.call_tool(name, args)
    text = res.content[0].text if res.content else "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error": "NOT_JSON", "message": text}


# ---------------------------------------------------------------- direct COM helpers (test setup only)
def features(doc):
    feat = try_call(doc, "FirstFeature")
    n = 0
    while feat is not None and n < 500:
        yield feat
        feat = try_call(feat, "GetNextFeature")
        n += 1


def select_point(doc, kind: str, x: float, y: float, z: float) -> bool:
    call(doc, "ClearSelection2", True)
    ext = call(doc, "Extension")
    return bool(call(ext, "SelectByID2", "", kind, x, y, z, False, 0, null_dispatch(), 0))


def build_block(app, folder: str) -> tuple[str, str]:
    """Create Block.SLDPRT: 40 x 20 mm rectangle on the Top plane, extruded 10 mm. Returns (path, depth dim name)."""
    template = try_call(app, "GetUserPreferenceStringValue", 8)  # swDefaultTemplatePart
    if not template:
        raise RuntimeError("No default part template is set (Tools > Options > Default Templates).")
    doc = call(app, "NewDocument", template, 0, 0.0, 0.0)
    if doc is None:
        raise RuntimeError(f"NewDocument failed with template {template}")
    planes = [f for f in features(doc) if try_call(f, "GetTypeName2") == "RefPlane"]
    if len(planes) < 3:
        raise RuntimeError("Could not find the 3 default planes")
    call(doc, "ClearSelection2", True)
    call(planes[1], "Select2", False, 0)  # Top plane, whatever its localized name
    sm = call(doc, "SketchManager")
    call(sm, "InsertSketch", True)
    try:
        sm.AddToDB = True
    except Exception:  # noqa: BLE001
        pass
    segs = call(sm, "CreateCornerRectangle", -0.02, -0.01, 0.0, 0.02, 0.01, 0.0)
    try:
        sm.AddToDB = False
    except Exception:  # noqa: BLE001
        pass
    if not segs:
        raise RuntimeError("CreateCornerRectangle returned nothing")
    call(sm, "InsertSketch", True)
    call(doc, "ClearSelection2", True)
    sketch = call(doc, "FeatureByPositionReverse", 0)
    call(sketch, "Select2", False, 0)
    fm = call(doc, "FeatureManager")
    feat = call(
        fm, "FeatureExtrusion3",
        True, False, False, 0, 0, 0.010, 0.0, False, False, False, False, 0.0, 0.0,
        False, False, False, False, True, True, True, 0, 0.0, False,
    )
    if feat is None:
        raise RuntimeError("FeatureExtrusion3 returned nothing")
    depth_name = f"D1@{call(feat, 'Name')}"
    path = os.path.join(folder, "Block.SLDPRT")
    ok, (err, _warn) = call_with_out_ints(call(doc, "Extension"), "SaveAs", path, 0, 1, null_dispatch())
    if not ok or err:
        raise RuntimeError(f"SaveAs failed, error code {err}")
    call(app, "CloseDoc", call(doc, "GetTitle"))
    return path, depth_name


# ---------------------------------------------------------------- the test run
async def run(args) -> None:
    record("environment", "INFO", f"Python {platform.python_version()}, sw_mcp {__version__}, Windows {platform.version()}")
    exe = connection.find_solidworks_exe()
    record("SolidWorks registered", "PASS" if exe else "FAIL", exe or "SldWorks.Application is not registered")
    record("SolidWorks processes", "INFO", str(connection.solidworks_pids()) or "none")

    projects = tempfile.mkdtemp(prefix="sw_mcp_projects_")  # never touch the user's real projects
    server = StdioServerParameters(command=sys.executable, args=["-m", "sw_mcp"], cwd=ROOT,
                                   env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src"),
                                        "SW_MCP_PROJECTS": projects})
    started = time.monotonic()
    async with Client(server, read_timeout_seconds=180) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        record("server starts and lists tools", "PASS" if len(names) >= 5 else "FAIL",
               f"{time.monotonic() - started:.1f}s: {names}")

        # --- get_status (may start SolidWorks)
        status = await tool(client, "get_status")
        deadline = time.monotonic() + 240
        while not status.get("ok") and status.get("error") == "SW_STARTING" and time.monotonic() < deadline:
            print("        SolidWorks is starting, waiting 15 s ...", flush=True)
            await asyncio.sleep(15)
            status = await tool(client, "get_status")
        record("get_status", "PASS" if status.get("ok") else "FAIL", short(status))
        if not status.get("ok"):
            return

        # --- prepare a test part in a temp folder
        work = tempfile.mkdtemp(prefix="sw_mcp_smoke_")
        pythoncom.CoInitialize()
        app = connection._attach_running()
        depth_name = None
        try:
            if args.part:
                part = os.path.join(work, os.path.basename(args.part))
                shutil.copy2(args.part, part)
                record("copy your part", "PASS", part)
            else:
                part, depth_name = build_block(app, work)
                record("build test block (direct COM)", "PASS", f"{part}, depth dimension {depth_name}")
        except Exception as exc:  # noqa: BLE001
            record("prepare test part", "FAIL", f"{exc}. Tip: re-run with --part \"C:\\path\\to\\any.SLDPRT\"")
            return

        # --- open_document
        opened = await tool(client, "open_document", file_path=part)
        record("open_document", "PASS" if opened.get("ok") else "FAIL", short(opened))
        doc = try_call(app, "ActiveDoc")

        # --- select a face, read the context
        if args.part:
            input("\n>>> In SolidWorks, click ONE face of the part, then press Enter here... ")
        else:
            if not (select_point(doc, "FACE", 0.0, 0.010, 0.0) or select_point(doc, "FACE", 0.0, -0.010, 0.0)):
                record("select top face (direct COM)", "FAIL", "SelectByID2 found no face at y=+/-10 mm")
        ctx = await tool(client, "get_selection_context")
        face = (ctx.get("items") or [{}])[0]
        ok = ctx.get("ok") and face.get("type") == "face" and face.get("surface")
        record("get_selection_context (face)", "PASS" if ok else "FAIL", short(ctx))
        if not args.part and face.get("normal"):
            n = face["normal"]
            record("face normal direction", "INFO",
                   f"top face normal {n} (expected about [0, 1, 0] if normals point out of the material)")
        dims = face.get("dims") or []
        if not depth_name:
            depth_name = dims[0]["name"] if dims else None
        listed = any(d.get("name") == depth_name for d in dims)
        record("context lists the feature dimensions", "PASS" if listed else "FAIL",
               f"looking for {depth_name} in {[d.get('name') for d in dims]}")

        # --- set_dimension
        if depth_name:
            old = next((d["value"] for d in dims if d.get("name") == depth_name), None)
            target = 25.0 if not args.part else round((old or 10) * 1.1, 3)
            changed = await tool(client, "set_dimension", dimension_name=depth_name, new_value=target)
            good = changed.get("ok") and changed.get("new") is not None and math.isclose(changed["new"], target, abs_tol=1e-3)
            record("set_dimension", "PASS" if good else "FAIL", short(changed))
            if not args.part:
                picked = select_point(doc, "FACE", 0.0, 0.025, 0.0) or select_point(doc, "FACE", 0.0, -0.025, 0.0)
                again = await tool(client, "get_selection_context")
                val = next((d["value"] for d in ((again.get("items") or [{}])[0].get("dims") or [])
                            if d.get("name") == depth_name), None)
                record("model really changed (face moved, dim reads 25)", "PASS" if picked and val == 25.0 else "FAIL",
                       short(again, 300))
                edge_ok = select_point(doc, "EDGE", 0.020, 0.025, 0.0) or select_point(doc, "EDGE", 0.020, -0.025, 0.0)
                edge = await tool(client, "get_selection_context")
                item = (edge.get("items") or [{}])[0]
                good = edge_ok and item.get("type") == "edge" and item.get("length_mm") == 20.0
                record("get_selection_context (edge, 20 mm)", "PASS" if good else "FAIL", short(edge, 300))
        else:
            record("set_dimension", "SKIP", "no dimension found on the selected face")

        # --- save / export
        export = os.path.join(work, "export.step")
        exported = await tool(client, "save_document", save_as_path=export)
        record("save_document (export STEP)", "PASS" if exported.get("ok") and os.path.isfile(export) else "FAIL",
               short(exported))
        exists = await tool(client, "save_document", save_as_path=export)
        record("save_document refuses to overwrite", "PASS" if exists.get("error") == "FILE_EXISTS" else "FAIL",
               short(exists))
        saved = await tool(client, "save_document")
        record("save_document (in place)", "PASS" if saved.get("ok") else "FAIL", short(saved))

        # --- error paths the LLM will hit
        missing = await tool(client, "open_document", file_path=os.path.join(work, "does_not_exist.SLDPRT"))
        record("clear error: missing file", "PASS" if missing.get("error") == "FILE_NOT_FOUND" else "FAIL", short(missing))
        bad = await tool(client, "set_dimension", dimension_name="D99@NoSuchFeature", new_value=5)
        record("clear error: unknown dimension", "PASS" if bad.get("error") == "NOT_FOUND" else "FAIL", short(bad))

        try_call(app, "CloseDoc", try_call(doc, "GetTitle"))
        await build_checks(client, work)
        record("temp folder", "INFO", work)


async def build_checks(client: Client, work: str) -> None:
    """Build a part from scratch with the modeling tools and check every result."""
    async def step(label: str, name: str, check=None, **args) -> dict:
        out = await tool(client, name, **args)
        ok = out.get("ok") and (check is None or check(out))
        record(f"build: {label}", "PASS" if ok else "FAIL", short(out, 400))
        return out

    created = await step("new_part", "new_part")
    if not created.get("ok"):
        return
    await step("plate 60x40x10", "make_box",
               lambda o: o.get("size_mm") == [60.0, 10.0, 40.0] and abs(o["volume_mm3"] - 24000) < 1,
               mode="add", x_min_mm=-30, x_max_mm=30, y_min_mm=0, y_max_mm=10, z_min_mm=-20, z_max_mm=20)
    await step("fillet 4 vertical corners R3", "finish_edges",
               lambda o: o.get("edges") == 4 and -80 < o.get("volume_change_mm3", 0) < -75,
               kind="fillet", size_mm=3, edges="vertical")
    await step("hole d6 at x=-22 z=-12", "make_cylinder",
               lambda o: abs(o.get("volume_change_mm3", 0) + 282.7) < 3,
               mode="cut", start_x_mm=-22, start_y_mm=-1, start_z_mm=-12, end_x_mm=-22, end_y_mm=11,
               end_z_mm=-12, diameter_mm=6)
    await step("repeat hole along X (row of 2)", "repeat_last_shape",
               lambda o: o.get("group_size") == 2 and abs(o.get("volume_change_mm3", 0) + 282.7) < 3,
               copies=1, step_x_mm=44)
    await step("repeat the row along Z (grid of 4)", "repeat_last_shape",
               lambda o: o.get("group_size") == 4 and abs(o.get("volume_change_mm3", 0) + 565.5) < 5,
               copies=1, step_z_mm=24)
    await step("pocket 20x10, 4 deep", "make_box",
               lambda o: abs(o.get("volume_change_mm3", 0) + 800) < 2,
               mode="cut", x_min_mm=-10, x_max_mm=10, y_min_mm=6, y_max_mm=10, z_min_mm=-5, z_max_mm=5)
    await step("boss d10 x 8 high at z=12", "make_cylinder",
               lambda o: o.get("size_mm") == [60.0, 18.0, 40.0] and abs(o["volume_change_mm3"] - 628.3) < 3,
               mode="add", start_x_mm=0, start_y_mm=10, start_z_mm=12, end_x_mm=0, end_y_mm=18,
               end_z_mm=12, diameter_mm=10)
    await step("angled prism cut on the +X end", "make_prism",
               lambda o: -730 < o.get("volume_change_mm3", 0) < -690,
               mode="cut", axis="z", points_mm="30,10; 30,4; 24,10", start_mm=-21, end_mm=21)
    await step("extra box then undo", "make_box", None,
               mode="add", x_min_mm=-5, x_max_mm=5, y_min_mm=18, y_max_mm=30, z_min_mm=10, z_max_mm=14)
    await step("undo_last_feature", "undo_last_feature", lambda o: o.get("size_mm") == [60.0, 18.0, 40.0])
    expected = 24000 - 77.3 - 4 * 282.74 - 800 + 628.3 - 720
    await step(f"summary (expect volume about {expected:.0f} mm3, 1 body)", "get_model_summary",
               lambda o: o.get("bodies") == 1 and abs(o.get("volume_mm3", 0) - expected) < 0.01 * expected
               and {"Box1", "Fillet1", "Hole1", "Hole2", "Hole3", "Hole4", "Pocket1", "Cylinder1", "Cut-Prism1"}
               <= {f.get("name") for f in o.get("features", [])})
    path = os.path.join(work, "Built.SLDPRT")
    await step("save the built part", "save_document", lambda o: os.path.isfile(path), save_as_path=path)

    # A flange: bolt circle with repeat_last_shape_around, a row of pins with repeat_last_shape.
    if not (await step("flange: new_part", "new_part")).get("ok"):
        return
    await step("flange: disc d90 x 10", "make_cylinder",
               lambda o: o.get("size_mm") == [90.0, 10.0, 90.0],
               mode="add", start_x_mm=0, start_y_mm=0, start_z_mm=0, end_x_mm=0, end_y_mm=10, end_z_mm=0,
               diameter_mm=90)
    await step("flange: bolt hole d8 at r=35", "make_cylinder",
               lambda o: abs(o.get("volume_change_mm3", 0) + 502.7) < 4,
               mode="cut", start_x_mm=35, start_y_mm=-1, start_z_mm=0, end_x_mm=35, end_y_mm=11, end_z_mm=0,
               diameter_mm=8)
    await step("flange: 5 more holes every 60 degrees", "repeat_last_shape_around",
               lambda o: len(o.get("features", [])) == 5 and abs(o.get("volume_change_mm3", 0) + 2513.3) < 15,
               copies=5, angle_step_deg=60)
    await step("flange: center bore d30", "make_cylinder",
               lambda o: abs(o.get("volume_change_mm3", 0) + 7068.6) < 30,
               mode="cut", start_x_mm=0, start_y_mm=-1, start_z_mm=0, end_x_mm=0, end_y_mm=11, end_z_mm=0,
               diameter_mm=30)
    await step("flange: pin d6 x 6 on top", "make_cylinder",
               lambda o: o.get("size_mm") == [90.0, 16.0, 90.0],
               mode="add", start_x_mm=-10, start_y_mm=10, start_z_mm=25, end_x_mm=-10, end_y_mm=16, end_z_mm=25,
               diameter_mm=6)
    await step("flange: 2 more pins in a row", "repeat_last_shape",
               lambda o: len(o.get("features", [])) == 2 and abs(o.get("volume_change_mm3", 0) - 339.3) < 4,
               copies=2, step_x_mm=10)
    flange = 63617.3 - 6 * 502.65 - 7068.6 + 3 * 169.65
    await step(f"flange: summary (expect about {flange:.0f} mm3, 1 body)", "get_model_summary",
               lambda o: o.get("bodies") == 1 and abs(o.get("volume_mm3", 0) - flange) < 0.01 * flange)

    # The same flange from ONE plan (what the agent does): one call, checked against "expect".
    plan = {"name": "PlanFlange", "steps": [
        {"op": "cylinder", "start": [0, 0, 0], "end": [0, 10, 0], "diameter": 90},
        {"op": "cylinder", "mode": "cut", "start": [35, -1, 0], "end": [35, 11, 0], "diameter": 8},
        {"op": "repeat_around", "copies": 5, "angle_step": 60},
        {"op": "cylinder", "mode": "cut", "start": [0, -1, 0], "end": [0, 11, 0], "diameter": 30},
        {"op": "cylinder", "start": [-10, 10, 25], "end": [-10, 16, 25], "diameter": 6},
        {"op": "repeat", "copies": 2, "step": [10, 0, 0]},
    ], "expect": {"size": [90, 16, 90], "bodies": 1}}
    await step("build_part: whole flange from one plan", "build_part",
               lambda o: o.get("check") == "matches the plan" and abs(o.get("volume_mm3", 0) - flange) < 0.01 * flange,
               plan=json.dumps(plan))
    bad = await tool(client, "build_part",
                     plan='{"steps":[{"op":"cylinder","start":[0,0,0],"end":[5,5,0],"diameter":3}]}')
    rejected = bad.get("error") == "BAD_ARGUMENT" and "Step 1 (cylinder)" in bad.get("message", "")
    record("build: build_part rejects a bad plan before building", "PASS" if rejected else "FAIL", short(bad, 400))

    # Phase 5 geometry: tilted features, revolve, a project with an assembly.
    block = {"op": "box", "x": [-60, 60], "y": [0, 50], "z": [-30, 30]}
    tilted = {"steps": [block, {"op": "cylinder", "mode": "cut", "start": [0, 20, 0], "end": [0, 120, 0],
                                "diameter": 30, "rotate": {"axis": "z", "deg": 45, "about": [0, 20, 0]}}]}
    await step("tilted bore (45 deg) cut into a block", "build_part",
               lambda o: o.get("bodies") == 1 and 360000 - 45000 < o.get("volume_mm3", 0) < 360000 - 20000,
               plan=json.dumps(tilted))
    disc = {"steps": [{"op": "revolve", "axis": "y", "profile": [[0, 0], [20, 0], [20, 10], [0, 10]]}]}
    await step("revolve a disc d40 x 10", "build_part",
               lambda o: abs(o.get("volume_mm3", 0) - 12566.4) < 130 and o.get("size_mm") == [40.0, 10.0, 40.0],
               plan=json.dumps(disc))
    await step("project: save part 'block'", "build_part", lambda o: bool(o.get("saved_as")),
               plan=json.dumps({"steps": [block]}), save_as="Smoke/block")
    pin = {"steps": [{"op": "cylinder", "start": [0, 50, 0], "end": [0, 90, 0], "diameter": 10}]}
    await step("project: save part 'pin' (sits on the block)", "build_part", lambda o: bool(o.get("saved_as")),
               plan=json.dumps(pin), save_as="Smoke/pin")
    await step("make_assembly puts both parts where they were designed", "make_assembly",
               lambda o: o.get("components") == ["block", "pin"] and not o.get("warnings")
               and o.get("size_mm") == [120.0, 90.0, 60.0],
               project="Smoke", name="Smoke assembly")


def write_report() -> None:
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    lines = [f"sw_mcp smoke test {time.strftime('%Y-%m-%d %H:%M:%S')}: {passed} passed, {failed} failed", ""]
    lines += [f"[{s}] {step}\n    {detail}" for step, s, detail in results]
    try:
        with open(config.LOG_FILE, encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-80:]
        lines += ["", f"---- last lines of server log {config.LOG_FILE} ----", *[t.rstrip() for t in tail]]
    except OSError:
        lines += ["", f"(no server log at {config.LOG_FILE})"]
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\n{passed} passed, {failed} failed. Report written to {REPORT}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--part", help="use a copy of this part instead of building a test block")
    args = parser.parse_args()
    if args.part and not os.path.isfile(args.part):
        parser.error(f"file not found: {args.part}")
    try:
        asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001
        record("unexpected crash", "FAIL", f"{exc}\n{traceback.format_exc()}")
    finally:
        write_report()


if __name__ == "__main__":
    main()
