"""The assistant loop: user message -> model (via the router) -> SolidWorks tools -> reply.

Robust against weak/free models on small free tiers (some allow only ~7000 tokens a minute):
- a lean tool set with compact descriptions (the full ones stay on the MCP server for other apps),
- "rescue" of tool calls the model wrote as text (JSON plan, <tool_call> blocks),
- a compact history: earlier requests become short notes, but the user's task is always kept,
- an identical call that already failed is not run again,
- a step limit per message so a confused model cannot loop forever ("continue" resumes).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from importlib import resources
from typing import Any, Callable

from .router import NoModelAvailable, Router

log = logging.getLogger("sw_agent.assistant")
TRANSCRIPT_CHARS = 8000

LEAN_TOOLS = (
    "get_status", "open_document", "save_document", "build_part", "get_model_summary",
    "get_selection_context", "set_dimension", "make_assembly", "list_project",
    "manage_documents", "make_engine", "connect_parts", "move_mechanism", "make_motion_study",
)
MAX_STEPS = 24
OLD_TURNS = 6               # earlier requests kept (as short notes) in each request
FULL_EXCHANGES = 2          # the newest tool calls/results of the current request are sent in full
TOOL_RESULT_CHARS = 6000    # the newest tool outputs are cut at this
LONG_ARGUMENT_CHARS = 240   # older tool-call arguments longer than this are replaced by a note

CHAT_PREAMBLE = """You are the SolidWorks Assistant. The user is an engineer, not a programmer.
Talk in short, plain sentences. When a request is clear, act with the tools right away.
"""


def system_prompt() -> str:
    guide = resources.files("sw_mcp").joinpath("guide.md").read_text(encoding="utf-8")
    return CHAT_PREAMBLE + "\n" + guide


# ------------------------------------------------------------ tools over MCP
class Toolbox:
    """Talks to the SolidWorks MCP server (in-process by default)."""

    def __init__(self, names: tuple[str, ...] | None = LEAN_TOOLS) -> None:
        self.names = names
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self.tools: list[dict] = []

    async def __aenter__(self) -> "Toolbox":
        from mcp import Client

        from sw_mcp.server import create_server

        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(Client(create_server()))
        listed = (await self._client.list_tools()).tools
        self.tools = [
            compact_tool({"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                           "parameters": t.input_schema}})
            for t in listed if self.names is None or t.name in self.names
        ]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    @property
    def tool_names(self) -> set[str]:
        return {t["function"]["name"] for t in self.tools}

    async def call(self, name: str, args: dict) -> str:
        if name not in self.tool_names:
            return json.dumps({"ok": False, "error": "UNKNOWN_TOOL",
                               "message": f"There is no tool named {name}.",
                               "fix": "Use one of: " + ", ".join(sorted(self.tool_names))})
        result = await self._client.call_tool(name, args)
        texts = [c.text for c in result.content if getattr(c, "text", None)]
        return "\n".join(texts) or json.dumps({"ok": not result.is_error})


def _first_sentence(text: str, limit: int = 110) -> str:
    text = " ".join((text or "").split())
    cut = text.find(". ")
    text = text[:cut + 1] if 0 < cut < limit else text
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def compact_tool(tool: dict) -> dict:
    """The same tool with short descriptions: every request carries all tool definitions, and the
    operating guide in the system prompt already explains the workflow."""
    fn = tool["function"]
    lines = [ln.strip() for ln in (fn.get("description") or "").splitlines()]
    if fn["name"] == "build_part":  # its step syntax is essential; the guide has the example
        kept = []
        for ln in lines:
            if ln.startswith("Example"):
                break
            if ln:
                kept.append(ln)
        desc = "\n".join(kept)
    else:
        paragraph = []
        for ln in lines:
            if not ln:
                break
            paragraph.append(ln)
        desc = " ".join(paragraph)
        use = next((ln for ln in lines if ln.startswith("Use when")), "")
        if use:
            desc += " " + use
    params = json.loads(json.dumps(fn.get("parameters") or {}))
    for prop in (params.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("description"):
            prop["description"] = _first_sentence(prop["description"])
    return {"type": "function", "function": {"name": fn["name"], "description": desc, "parameters": params}}


# ------------------------------------------------------------ rescuing text-only tool calls
_TOOL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _json_objects(text: str) -> list[dict]:
    found = []
    for pattern in (_TOOL_TAG, _JSON_BLOCK):
        for m in pattern.finditer(text):
            try:
                found.append(json.loads(m.group(1)))
            except ValueError:
                pass
    if not found:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                found.append(json.loads(text[start:end + 1]))
            except ValueError:
                pass
    return [f for f in found if isinstance(f, dict)]


def rescue_tool_calls(content: str, tool_names: set[str]) -> list[dict]:
    """Turn tool calls a model wrote as plain text into real tool calls."""
    calls = []
    for obj in _json_objects(content or ""):
        name = obj.get("name") or obj.get("tool")
        args = obj.get("arguments", obj.get("parameters", obj.get("args")))
        if name in tool_names and isinstance(args, (dict, str)):
            calls.append((name, args if isinstance(args, str) else json.dumps(args)))
        elif "steps" in obj and "build_part" in tool_names:
            calls.append(("build_part", json.dumps({"plan": json.dumps(obj)})))
    return [{"id": f"rescued_{uuid.uuid4().hex[:8]}", "type": "function",
             "function": {"name": n, "arguments": a}} for n, a in calls]


def parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def short_result(text: str) -> str:
    """An older tool result, reduced to what the model still needs."""
    try:
        data = json.loads(text)
    except ValueError:
        return text[:200]
    if isinstance(data, dict) and data.get("ok") is False:
        return json.dumps({k: str(data[k])[:300] for k in ("error", "message", "fix") if k in data})
    return summarize_result(text)


def _short_arguments(raw: str) -> str:
    out = {}
    for key, value in parse_args(raw).items():
        text = value if isinstance(value, str) else json.dumps(value)
        out[key] = value if len(text) <= LONG_ARGUMENT_CHARS else f"(sent earlier, {len(text)} characters)"
    return json.dumps(out)


def _num(v: Any) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def summarize_result(text: str) -> str:
    """One plain-language line for the screen, e.g. 'Part1: 100 x 12 x 60 mm, 6 features, matches the plan'."""
    try:
        data = json.loads(text)
    except ValueError:
        return text[:160]
    if not isinstance(data, dict):
        return text[:160]
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


# ------------------------------------------------------------ the loop
@dataclass
class Event:
    kind: str  # thinking | tool | result | model | error | reply
    text: str


class Assistant:
    def __init__(self, router: Router, toolbox: Toolbox, on_event: Callable[[Event], None] | None = None,
                 max_steps: int = MAX_STEPS, transcript: Any = None) -> None:
        self.router = router
        self.toolbox = toolbox
        self.transcript = transcript
        show = on_event or (lambda e: None)

        def emit(event: Event) -> None:
            if self.transcript is not None and event.kind != "thinking":
                self.transcript.write(event.kind, event.text)
            show(event)

        self.on_event = emit
        self.max_steps = max_steps
        self.system = system_prompt()
        self.history: list[dict] = []
        router.on_event = lambda msg: self.on_event(Event("model", msg))

    def reset(self) -> None:
        self.history.clear()
        if self.transcript is not None:
            self.transcript.write("info", "new conversation")

    def _window(self) -> list[dict]:
        """System prompt + a compact history. Earlier requests become (user text, a short note of
        what the tools did, the reply); the current request is sent in full, except that tool
        results and long arguments older than the newest FULL_EXCHANGES exchanges are shortened."""
        out = [{"role": "system", "content": self.system}]
        users = [i for i, m in enumerate(self.history) if m.get("role") == "user"]
        if not users:
            return out
        names = {c.get("id"): c.get("function", {}).get("name", "?")
                 for m in self.history for c in (m.get("tool_calls") or [])}
        starts = users[-(OLD_TURNS + 1):]
        for a, b in zip(starts, starts[1:]):
            turn = self.history[a:b]
            notes, reply = [], ""
            for msg in turn[1:]:
                if msg.get("role") == "tool":
                    notes.append(f"{names.get(msg.get('tool_call_id'), 'tool')}: {short_result(msg.get('content') or '')}")
                elif msg.get("role") == "assistant" and msg.get("content") and not msg.get("tool_calls"):
                    reply = msg["content"]
            note = ("Tools used: " + " | ".join(notes[-8:]) + "\n") if notes else ""
            out.append({"role": "user", "content": (turn[0].get("content") or "")[:1200]})
            out.append({"role": "assistant", "content": (note + reply)[:2000] or "(no answer)"})
        current = self.history[starts[-1]:]
        exchanges = [i for i, m in enumerate(current) if m.get("role") == "assistant" and m.get("tool_calls")]
        cutoff = exchanges[-FULL_EXCHANGES] if len(exchanges) >= FULL_EXCHANGES else (exchanges[0] if exchanges else 0)
        for i, msg in enumerate(current):
            if i < cutoff and msg.get("role") == "tool":
                msg = {**msg, "content": short_result(msg.get("content") or "")}
            elif i < cutoff and msg.get("tool_calls"):
                msg = {**msg, "tool_calls": [{**c, "function": {**c["function"], "arguments": _short_arguments(
                    c["function"].get("arguments") or "{}")}} for c in msg["tool_calls"]]}
            out.append(msg)
        return out

    async def send(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})
        if self.transcript is not None:
            self.transcript.write("user", text)
        log.info("user message (%d chars)", len(text))
        getattr(self.router, "new_turn", lambda: None)()
        tools = self.toolbox.tools
        failed: dict[str, str] = {}  # identical calls that already failed in this request
        same_error: dict[str, int] = {}  # tool + error (numbers ignored) -> how often it happened
        for _ in range(self.max_steps):
            self.on_event(Event("thinking", "thinking"))
            try:
                result, cand = await asyncio.to_thread(self.router.chat, self._window(), tools)
            except NoModelAvailable as exc:
                log.warning("no model available: %s", exc)
                self.on_event(Event("error", str(exc)))
                return str(exc)
            if self.transcript is not None:
                self.transcript.write("model_answer", result.content[:TRANSCRIPT_CHARS], model=cand.label,
                                      latency_ms=result.latency_ms, usage=result.usage,
                                      tool_calls=len(result.tool_calls))
            calls = result.tool_calls or rescue_tool_calls(result.content, self.toolbox.tool_names)
            if not calls:
                reply = result.content.strip() or "(no answer)"
                self.history.append({"role": "assistant", "content": reply})
                self.on_event(Event("reply", reply))
                return reply
            calls = [{**{k: v for k, v in c.items() if k not in ("id", "type", "function", "index")},
                      "id": c.get("id") or f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                      "function": {"name": c["function"]["name"],
                                   "arguments": c["function"].get("arguments") if isinstance(
                                       c["function"].get("arguments"), str)
                                   else json.dumps(c["function"].get("arguments") or {})}} for c in calls]
            self.history.append({"role": "assistant", "content": result.content or None, "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                args = parse_args(call["function"]["arguments"])
                if self.transcript is not None:
                    self.transcript.write("tool_call", name, arguments=json.dumps(args)[:TRANSCRIPT_CHARS])
                self.on_event(Event("tool", name))
                key = name + json.dumps(args, sort_keys=True)
                stuck = [k for k, n in same_error.items() if k.startswith(name + "|") and n >= 2]
                if key in failed:
                    output = json.dumps({"ok": False, "error": "REPEATED_CALL",
                                         "message": f"This exact call already failed: {failed[key]}",
                                         "fix": "Change what the error names, or use another approach. "
                                                "Never send the same failing call twice."})
                elif stuck:
                    output = json.dumps({"ok": False, "error": "STOP_RETRYING",
                                         "message": f"{name} already failed twice with the same error; "
                                                    "changing the numbers did not help.",
                                         "fix": "Stop. Tell the user what failed, in plain words, and ask how to "
                                                "continue (or to send the logs from the Tools menu)."})
                else:
                    output = await self.toolbox.call(name, args)
                    try:
                        data = json.loads(output)
                    except ValueError:
                        data = None
                    if isinstance(data, dict) and data.get("ok") is False:
                        failed[key] = str(data.get("message", ""))[:300]
                        gist = name + "|" + re.sub(r"[-\d.]+", "#", str(data.get("message", "")))[:200]
                        same_error[gist] = same_error.get(gist, 0) + 1
                if self.transcript is not None:
                    self.transcript.write("tool_output", output[:TRANSCRIPT_CHARS], tool=name)
                self.on_event(Event("result", summarize_result(output)))
                self.history.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": output[:TOOL_RESULT_CHARS]})
        reply = (f"I paused after {self.max_steps} steps without finishing. Say \"continue\" and I will "
                 "carry on from here.")
        self.history.append({"role": "assistant", "content": reply})
        self.on_event(Event("reply", reply))
        return reply
