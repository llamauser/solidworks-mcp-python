"""The shared picture of a job: <project>/design.json, kept by the program, not by any AI model.

With free providers the model changes often during one job, and every model used to rebuild the
design in its head from a shortened chat history, each one differently. Now every tool that
changes a project writes what it did here: the machine plan (a checklist of parts), each part's
plan, size and round features (shafts and bores: axis, diameter, position), the assembly and its
joints, failures, and a short event log. summary() turns it into a compact block that goes into
every request, so whichever model answers reads the same facts and continues the plan.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import modeling as m

MAX_EVENTS = 30
SUMMARY_CHARS = 2400


def _root() -> Path:
    from .project import projects_root

    return projects_root()


def _folder(project: str) -> Path:
    from .project import _clean

    return _root() / _clean(project)


def path(project: str) -> Path:
    return _folder(project) / "design.json"


def load(project: str) -> dict:
    try:
        data = json.loads(path(project).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(project: str, data: dict) -> None:
    target = path(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    data["project"] = target.parent.name
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def _event(data: dict, text: str) -> None:
    events = data.setdefault("events", [])
    events.append(f"{time.strftime('%H:%M')} {text}")
    del events[:-MAX_EVENTS]


def project_of_path(file_path: str) -> str | None:
    """The project a saved file belongs to (its folder, if it is in the projects folder)."""
    if not file_path:
        return None
    try:
        folder = Path(file_path).resolve().parent
        return folder.name if folder.parent == _root().resolve() else None
    except OSError:
        return None


# ---------------------------------------------------------------- writing
def parse_parts(text: str) -> list[dict]:
    """ "name: description" per line (or separated by ';') -> [{"name", "description"}]."""
    import re

    out = []
    for line in re.split(r"[\n;]+", text or ""):
        line = line.strip().lstrip("-*0123456789.) ").strip()
        if not line:
            continue
        name, _, desc = line.partition(":")
        name = re.sub(r"[^\w\- ]", "", name).strip().replace(" ", "_")
        if name:
            out.append({"name": name[:60], "description": desc.strip()})
    return out


def set_plan(project: str, goal: str, parts: list[dict], notes: str = "") -> dict:
    """The machine plan: the checklist every model follows. Keeps the status of parts already built."""
    data = load(project)
    old = {p["name"]: p for p in (data.get("plan") or {}).get("parts", []) if isinstance(p, dict) and p.get("name")}
    built = data.get("parts") or {}
    checklist = []
    for item in parts:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        entry = {"name": name, "description": str(item.get("description", "")).strip()[:300]}
        entry["status"] = (built.get(name) or {}).get("status") or old.get(name, {}).get("status") or "todo"
        checklist.append(entry)
    data["plan"] = {"goal": goal.strip()[:600], "parts": checklist, "notes": notes.strip()[:800]}
    _event(data, f"plan set: {len(checklist)} parts")
    _save(project, data)
    return data


def _plan_part(data: dict, name: str) -> dict | None:
    for item in (data.get("plan") or {}).get("parts", []):
        if item.get("name", "").lower() == name.lower():
            return item
    return None


def record_part(project: str, name: str, plan: dict | None, result: dict | None = None,
                error: str = "") -> None:
    data = load(project)
    parts = data.setdefault("parts", {})
    entry = parts.get(name, {})
    if error:
        entry.update(status="failed", error=error[:400])
        _event(data, f"{name} FAILED: {error[:160]}")
    else:
        entry = {"status": "built"}
        for key in ("size_mm", "min_mm", "max_mm", "bodies"):
            if result and key in result:
                entry[key] = result[key]
        _event(data, f"{name} built" + (f" {'x'.join(f'{v:g}' for v in result['size_mm'])} mm"
                                         if result and result.get("size_mm") else ""))
    if plan is not None:
        entry["plan"] = plan
        entry["round_features"] = round_features(plan)
    parts[name] = entry
    item = _plan_part(data, name)
    if item is not None:
        item["status"] = entry["status"]
    elif data.get("plan"):
        data["plan"]["parts"].append({"name": name, "description": "(added, not in the plan)",
                                      "status": entry["status"]})
    _save(project, data)


def record_assembly(project: str, name: str, components: list[str], size_mm: Any = None,
                    warnings: list[str] | None = None) -> None:
    data = load(project)
    data["assembly"] = {"name": name, "components": components, "size_mm": size_mm,
                        "joints": [], "fixed": [], "moving": []}
    _event(data, f"assembly {name}: {len(components)} parts" + (f", warnings: {'; '.join(warnings)[:160]}"
                                                                 if warnings else ""))
    _save(project, data)


def record_joints(project: str, joints: list[str], fixed: list[str], moving: list[str]) -> None:
    data = load(project)
    asm = data.setdefault("assembly", {"name": "", "components": []})
    asm.update(joints=joints, fixed=fixed, moving=moving)
    _event(data, f"connected: {len(joints)} joints, moving {', '.join(moving) or 'nothing'}")
    _save(project, data)


def record_event(project: str, text: str) -> None:
    data = load(project)
    _event(data, text)
    _save(project, data)


# ---------------------------------------------------------------- round features from a plan
def _fmt(v: float) -> str:
    return f"{round(v, 2):g}"


def _pt(p) -> str:
    return "(" + ", ".join(_fmt(v) for v in p) + ")"


def round_features(plan: dict) -> list[str]:
    """Shafts and bores of a part plan in plain words: what other parts must line up with."""
    from .plan import Plan, shape_of

    try:
        steps = Plan.model_validate(plan).steps
    except Exception:  # noqa: BLE001 - a plan that does not parse has nothing to show
        return []
    out = []
    for step in steps:
        try:
            shape = shape_of(step)
        except Exception:  # noqa: BLE001
            continue
        kind = "bore" if getattr(step, "mode", "add") == "cut" else "shaft"
        if isinstance(shape, m.Shape) and shape.profile.kind == "circle":
            (a, b), r = shape.profile.points[0], shape.profile.radius
            p1, p2 = shape.world_point(a, b, shape.start), shape.world_point(a, b, shape.end)
            if shape.tilt:
                axis, deg, about = shape.tilt
                p1, p2 = m.rotate_point(p1, axis, deg, about), m.rotate_point(p2, axis, deg, about)
            out.append(f"{kind} d{_fmt(2 * r)} from {_pt(p1)} to {_pt(p2)}")
        elif isinstance(shape, m.Revolve):
            radii = [r for r, _ in shape.profile]
            hs = [h for _, h in shape.profile]
            inner = min(radii)
            text = f"round {kind} about {shape.axis.upper()} through {_pt(shape.center)}, outer d{_fmt(2 * max(radii))}"
            if inner > 1e-6:
                text += f", hole d{_fmt(2 * inner)}"
            out.append(text + f", {shape.axis} {_fmt(min(hs))}..{_fmt(max(hs))}")
    return out[:10]


# ---------------------------------------------------------------- the block every request gets
def summary(project: str, max_chars: int = SUMMARY_CHARS) -> str:
    data = load(project)
    if not data:
        return ""
    lines = [f'CURRENT DESIGN of project "{data.get("project", project)}" (kept by the program and shared by '
             "every AI model on this job; earlier steps may have been done by another model: continue from here, "
             "do not start over or rebuild finished parts unless they are wrong):"]
    plan = data.get("plan") or {}
    if plan.get("goal"):
        lines.append(f"Goal: {plan['goal']}")
    if plan.get("parts"):
        todo = [p["name"] for p in plan["parts"] if p.get("status") != "built"]
        lines.append("Checklist: " + " | ".join(
            f"{p['name']} {p.get('status', 'todo').upper() if p.get('status') == 'failed' else p.get('status', 'todo')}"
            + (f" ({p['description']})" if p.get("description") and p.get("status") != "built" else "")
            for p in plan["parts"]))
        lines.append("Next: " + (todo[0] if todo else "all parts built; make_assembly, then connect_parts"))
    if plan.get("notes"):
        lines.append(f"Notes: {plan['notes']}")
    parts = data.get("parts") or {}
    if parts:
        lines.append("Built parts (world coordinates, mm):")
        for name, p in parts.items():
            if p.get("status") == "failed":
                lines.append(f"- {name}: FAILED: {p.get('error', '')[:200]}")
                continue
            box = ""
            if p.get("min_mm") and p.get("max_mm"):
                box = " ".join(f"{ax} {_fmt(lo)}..{_fmt(hi)}" for ax, lo, hi in zip("xyz", p["min_mm"], p["max_mm"]))
            feats = "; ".join(p.get("round_features") or [])
            lines.append(f"- {name}: {box}" + (f"; {feats}" if feats else ""))
    asm = data.get("assembly")
    if asm:
        joints = asm.get("joints") or []
        lines.append(f"Assembly {asm.get('name', '')}: {len(asm.get('components') or [])} parts, "
                     f"{len(joints)} joints" + (f" ({'; '.join(joints[:8])})" if joints else "")
                     + (f", moving: {', '.join(asm.get('moving') or [])}" if asm.get("moving") else ""))
    events = data.get("events") or []
    if events:
        lines.append("Recent: " + " | ".join(events[-5:]))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 20].rsplit("\n", 1)[0] + "\n(...shortened)"
    return text


def exists(project: str) -> bool:
    return path(project).exists()
