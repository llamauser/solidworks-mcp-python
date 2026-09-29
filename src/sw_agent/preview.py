"""Plain-words views of SolidWorks tool calls for the page: a one-line summary of each result, and
a readable preview of a build_part plan or a make_engine call (no SolidWorks needed)."""

from __future__ import annotations

import json
from typing import Any


def preview_call(name: str, args: dict) -> dict | None:
    """What a build_part plan or make_engine call builds, step by step; None for other tools."""
    try:
        if name == "build_part":
            from sw_mcp.sw import plan

            save_as = str(args.get("save_as") or "").replace("\\", "/")
            return plan.preview(str(args.get("plan", "")), save_as.split("/")[0] if "/" in save_as else None)
        if name == "make_engine":
            from sw_mcp.sw import engine

            return engine.preview(args)
    except Exception:  # noqa: BLE001 - a preview is a bonus
        return None
    return None


def _num(v: Any) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def summarize_result(text: str) -> str:
    """One line for the screen, e.g. 'done: Part1, 100 x 12 x 60 mm, 6 features, matches the plan'."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return str(text)[:160]
    if not isinstance(data, dict):
        return str(text)[:160]
    if data.get("ok") is False:
        return f"problem: {data.get('message', '')}"[:220]
    parts: list[str] = []
    name = data.get("part") or data.get("feature") or data.get("created") or data.get("engine")
    if isinstance(data.get("opened"), dict):
        name = data["opened"].get("name")
    if name:
        parts.append(str(name))
    if isinstance(data.get("size_mm"), list):
        parts.append(" x ".join(_num(v) for v in data["size_mm"]) + " mm")
    features = data.get("features")
    if isinstance(features, int):
        parts.append(f"{features} features")
    elif isinstance(features, list) and features and isinstance(features[0], str):
        parts.append(f"{len(features)} copies")
    if "dimension" in data:
        parts.append(f"{data['dimension']}: {_num(data.get('old'))} -> {_num(data.get('new'))} {data.get('unit', '')}".strip())
    for key, label in (("saved_as", "saved to"), ("saved", "saved to"), ("exported", "exported to"),
                       ("assembly", "assembly saved to")):
        if data.get(key):
            parts.append(f"{label} {data[key]}")
    if "version" in data:
        parts.append(f"SolidWorks {data['version']}")
    if "selected_count" in data:
        parts.append(f"{data['selected_count']} selected")
    if isinstance(data.get("components"), list):
        parts.append(f"{len(data['components'])} parts")
    if isinstance(data.get("parts"), dict):
        parts.append(f"{len(data['parts'])} parts, {data.get('joints', 0)} joints")
    if isinstance(data.get("moved"), list):
        parts.append("moved: " + ", ".join(f"{m.get('part')} {m.get('travel_mm')} mm" for m in data["moved"][:6]))
    if data.get("check"):
        parts.append(str(data["check"]))
    if data.get("warning"):
        parts.append(f"note: {data['warning']}")
    return ("done: " + ", ".join(parts))[:260] if parts else "done"
