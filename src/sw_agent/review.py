"""Previews of a tool call for the person reviewing it (no SolidWorks needed)."""

from __future__ import annotations

import json
from typing import Any


def preview_call(name: str, args: dict) -> dict | None:
    """A plain-words preview of what the call would build, or None if there is nothing to show."""
    if name == "build_part":
        from sw_mcp.sw import plan

        return plan.preview(str(args.get("plan", "")))
    if name == "make_engine":
        from sw_mcp.sw import engine

        return engine.preview(args)
    if name == "run_command":
        from .extras import working_folder

        return {"ok": True, "command": str(args.get("command", "")), "runs_in": working_folder(),
                "warning": "This runs on your PC. Only run it if you understand it and asked for it."}
    return None


def edited_args(original: dict, edited: Any) -> dict:
    """The arguments the user sent back. A build_part plan may come back as an object; the tool
    takes it as a JSON string."""
    if not isinstance(edited, dict):
        return original
    out = dict(edited)
    if "plan" in out and not isinstance(out["plan"], str):
        out["plan"] = json.dumps(out["plan"])
    return out
