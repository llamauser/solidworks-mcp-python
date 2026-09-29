"""Job records: what was asked, what every model did, and how good the result was.

Recorded on this PC by default (%APPDATA%\\sw_agent\\jobs\\<id>\\record.json + picture). Nothing is
uploaded: `sw-agent share` (or the browser's "Share my builds") packs them into one zip that the
user sends to the developer. Keys never appear here; file paths, the Windows user name and e-mail
addresses are removed before a record is written.

A record holds the request, every step (tool, arguments, result, the model that made it, time),
the user's corrections (edited plans, skips with notes, stops), the final design numbers, a
picture of the result, the app/guide/tool versions, and the user's rating when given.
Highly rated records are also reused: a similar new request gets their plan as an example.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import config_dir

ARG_CHARS = 20000       # a whole plan fits
RESULT_CHARS = 3000
EXAMPLE_CHARS = 1600    # how much of a good plan is shown to the model as an example
TAGS = ("wrong size", "wrong shape", "missing part", "does not move", "parts misplaced", "too slow", "other")


def jobs_dir() -> Path:
    return config_dir() / "jobs"


# ---------------------------------------------------------------- removing personal details
_USER = re.compile(r"(?i)([a-z]:[\\/]{1,2}users[\\/]{1,2})[^\\/\s\"']+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def scrub(text: str) -> str:
    """Remove the Windows user name, the user's home folder and e-mail addresses."""
    if not text:
        return text
    home = os.path.expanduser("~")
    for variant in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
        if variant and len(variant) > 3:
            text = text.replace(variant, "~")
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    text = _USER.sub(lambda m: m.group(1) + "USER", text)
    if len(user) >= 3:
        text = re.sub(re.escape(user), "USER", text, flags=re.I)
    return _EMAIL.sub("EMAIL", text)


def _scrub_obj(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrub_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_obj(v) for k, v in value.items()}
    return value


def versions() -> dict:
    from importlib import resources

    from sw_mcp import __version__ as mcp_version

    guide = resources.files("sw_mcp").joinpath("guide.md").read_text(encoding="utf-8")
    return {"app": mcp_version, "guide": hashlib.sha1(guide.encode()).hexdigest()[:10]}


# ---------------------------------------------------------------- one job
class Job:
    """One user request and everything that happened for it."""

    def __init__(self, request: str, interface: str = "", tools: list[dict] | None = None) -> None:
        self.id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.started = time.monotonic()
        self.data: dict[str, Any] = {
            "id": self.id, "time": datetime.now().isoformat(timespec="seconds"), "interface": interface,
            "request": request, "steps": [], "models": [], "user_actions": [], "rating": None,
            "versions": versions(),
            "tools_hash": hashlib.sha1(json.dumps(tools or [], sort_keys=True).encode()).hexdigest()[:10],
        }
        self.changed = False  # did anything change in SolidWorks?

    def model(self, label: str, usage: dict | None) -> None:
        self.data["models"].append({"model": label, "tokens_in": (usage or {}).get("prompt_tokens"),
                                    "tokens_out": (usage or {}).get("completion_tokens")})

    def step(self, tool: str, args: dict, output: str, model: str, seconds: float) -> None:
        try:
            result = json.loads(output)
        except ValueError:
            result = {"raw": output[:RESULT_CHARS]}
        ok = not (isinstance(result, dict) and result.get("ok") is False)
        text = json.dumps(result)
        self.data["steps"].append({
            "tool": tool, "model": model, "seconds": round(seconds, 1), "ok": ok,
            "args": {k: (v if len(json.dumps(v)) <= ARG_CHARS else "(too long)") for k, v in args.items()},
            "result": result if len(text) <= RESULT_CHARS else {"summary": text[:RESULT_CHARS]},
        })
        if ok and tool in CHANGES_SOLIDWORKS:
            self.changed = True

    def user_action(self, kind: str, tool: str, before: dict | None = None, after: dict | None = None,
                    note: str = "") -> None:
        """What the user did in review: edited (before/after = the model's plan and the corrected one),
        skipped (with a note), or stopped."""
        entry: dict[str, Any] = {"kind": kind, "tool": tool}
        if before is not None:
            entry["model_version"] = before
        if after is not None:
            entry["user_version"] = after
        if note:
            entry["note"] = note
        self.data["user_actions"].append(entry)

    def finish(self, reply: str, project: str | None, design: dict | None, picture: str | None) -> Path:
        self.data["reply"] = reply
        self.data["seconds"] = round(time.monotonic() - self.started, 1)
        self.data["changed_solidworks"] = self.changed
        if project:
            self.data["project"] = project
        if design:
            self.data["result"] = {
                "plan": design.get("plan"),
                "parts": {n: {k: p.get(k) for k in ("status", "size_mm", "min_mm", "max_mm", "bodies",
                                                     "round_features", "error") if p.get(k) is not None}
                          for n, p in (design.get("parts") or {}).items()},
                "assembly": design.get("assembly"),
            }
        folder = jobs_dir() / self.id
        folder.mkdir(parents=True, exist_ok=True)
        if picture and os.path.isfile(picture):
            self.data["picture"] = os.path.basename(picture)
        write_record(folder, self.data)
        return folder


CHANGES_SOLIDWORKS = {"build_part", "make_engine", "make_assembly", "connect_parts", "move_mechanism",
                      "make_motion_study", "set_dimension", "new_part", "make_box", "make_cylinder",
                      "make_prism", "repeat_last_shape", "repeat_last_shape_around", "finish_edges",
                      "undo_last_feature"}


def write_record(folder: Path, data: dict) -> None:
    tmp = folder / "record.tmp"
    tmp.write_text(json.dumps(_scrub_obj(data), indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, folder / "record.json")


def load(job_id: str) -> dict | None:
    try:
        return json.loads((jobs_dir() / job_id / "record.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def rate(job_id: str, stars: int, tags: list[str] | None = None, comment: str = "") -> dict:
    """The user's verdict on a job: 1-5 stars, quick tags and an optional comment."""
    if not re.fullmatch(r"[\w-]{6,60}", job_id or ""):
        raise ValueError("unknown job")
    data = load(job_id)
    if data is None:
        raise ValueError("unknown job")
    stars = max(1, min(5, int(stars)))
    data["rating"] = {"stars": stars, "tags": [t for t in (tags or []) if t in TAGS],
                      "comment": comment.strip()[:1000], "time": datetime.now().isoformat(timespec="seconds")}
    write_record(jobs_dir() / job_id, data)
    return data["rating"]


def all_records() -> list[dict]:
    out = []
    for f in sorted(jobs_dir().glob("*/record.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- sharing
def pack(dest_folder: Path | None = None) -> tuple[Path, int]:
    """Zip every job record (and its picture) for the developer. Returns (zip path, number of jobs)."""
    desktop = Path(os.path.expanduser("~")) / "Desktop"
    dest_folder = dest_folder or (desktop if desktop.exists() else Path.cwd())
    zip_path = dest_folder / f"SolidWorks Assistant builds {datetime.now():%Y-%m-%d %H%M}.zip"
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in sorted(p for p in jobs_dir().glob("*") if p.is_dir()):
            record = folder / "record.json"
            if not record.exists():
                continue
            count += 1
            zf.write(record, f"{folder.name}/record.json")
            for pic in folder.glob("picture.*"):
                zf.write(pic, f"{folder.name}/{pic.name}")
        zf.writestr("README.txt", "SolidWorks Assistant build records: one folder per request, with what was asked, "
                                  "the steps each AI model took, the result, a picture and your rating.\n"
                                  "No API keys, file paths or user names are included.\n")
    return zip_path, count


# ---------------------------------------------------------------- reusing good results
_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("a an and the of to in on with for at by mm is it make build me i want please from as be".split())


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def similar_example(request: str, min_stars: int = 4) -> str:
    """The plan of the best-rated past job whose request is most like this one ("" if none)."""
    wanted = _words(request)
    if len(wanted) < 2:
        return ""
    best, best_score = None, 0.0
    for rec in all_records():
        rating = rec.get("rating") or {}
        if (rating.get("stars") or 0) < min_stars:
            continue
        words = _words(rec.get("request", ""))
        if not words:
            continue
        score = len(wanted & words) / len(wanted | words)
        if score > best_score:
            best, best_score = rec, score
    if best is None or best_score < 0.25:
        return ""
    calls = [s for s in best.get("steps", []) if s.get("ok") and s["tool"] in ("plan_machine", "build_part",
                                                                             "make_engine")]
    if not calls:
        return ""
    lines = [f'A similar past request, rated {best["rating"]["stars"]}/5 by the user: "{best["request"][:200]}". '
             "What worked then (adapt it, do not copy blindly):"]
    for s in calls:
        lines.append(f"{s['tool']} {json.dumps(s['args'])}")
    text = "\n".join(lines)
    return text if len(text) <= EXAMPLE_CHARS else text[:EXAMPLE_CHARS - 12] + " ...(cut)"
