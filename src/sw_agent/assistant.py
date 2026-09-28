"""The assistant loop: user message -> model (via the router) -> SolidWorks tools -> reply.

Robust against weak/free models:
- a lean tool set by default (fewer tokens per request),
- "rescue" of tool calls the model wrote as text (JSON plan, <tool_call> blocks),
- old tool results are shortened so the history stays small,
- a step limit per message so a confused model cannot loop forever.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from importlib import resources
from typing import Any, Callable

from .router import NoModelAvailable, Router

LEAN_TOOLS = (
    "get_status", "open_document", "save_document", "build_part", "get_model_summary",
    "get_selection_context", "set_dimension", "undo_last_feature", "make_assembly", "list_project",
)
MAX_STEPS = 15
KEEP_MESSAGES = 24          # conversation messages sent with each request (plus the system prompt)
OLD_TOOL_RESULT_CHARS = 400  # older tool outputs are shortened to this
TOOL_RESULT_CHARS = 6000     # the newest tool outputs are cut at this

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
            {"type": "function", "function": {"name": t.name, "description": t.description or "",
                                              "parameters": t.input_schema}}
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


def summarize_result(text: str) -> str:
    """One line for the screen."""
    try:
        data = json.loads(text)
    except ValueError:
        return text[:160]
    if not isinstance(data, dict):
        return text[:160]
    if data.get("ok") is False:
        return f"problem: {data.get('message', '')}"[:220]
    parts = []
    for key in ("feature", "part", "created", "opened", "saved", "saved_as", "exported", "assembly", "dimension",
                "size_mm", "features", "check", "selected_count", "version"):
        if key in data:
            value = data[key]
            if isinstance(value, dict):
                value = value.get("name", value)
            if isinstance(value, list) and len(value) > 6:
                value = f"{len(value)} items"
            parts.append(f"{key} {value}")
    return ("done: " + ", ".join(parts))[:220] if parts else "done"


# ------------------------------------------------------------ the loop
@dataclass
class Event:
    kind: str  # thinking | tool | result | model | error | reply
    text: str


class Assistant:
    def __init__(self, router: Router, toolbox: Toolbox, on_event: Callable[[Event], None] | None = None,
                 max_steps: int = MAX_STEPS) -> None:
        self.router = router
        self.toolbox = toolbox
        self.on_event = on_event or (lambda e: None)
        self.max_steps = max_steps
        self.system = system_prompt()
        self.history: list[dict] = []
        router.on_event = lambda msg: self.on_event(Event("model", msg))

    def reset(self) -> None:
        self.history.clear()

    def _window(self) -> list[dict]:
        """System prompt + recent history, starting at a user message, with old tool output shortened."""
        recent = self.history[-KEEP_MESSAGES:]
        while recent and recent[0].get("role") != "user":
            recent = recent[1:]
        if not recent and self.history:
            last_user = max(i for i, m in enumerate(self.history) if m.get("role") == "user")
            recent = self.history[last_user:]
        latest_user = max((i for i, m in enumerate(recent) if m.get("role") == "user"), default=0)
        out = [{"role": "system", "content": self.system}]
        for i, msg in enumerate(recent):
            if msg.get("role") == "tool" and i < latest_user and len(msg.get("content") or "") > OLD_TOOL_RESULT_CHARS:
                msg = {**msg, "content": msg["content"][:OLD_TOOL_RESULT_CHARS] + " ...(shortened)"}
            out.append(msg)
        return out

    async def send(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})
        tools = self.toolbox.tools
        for _ in range(self.max_steps):
            self.on_event(Event("thinking", "thinking"))
            try:
                result, cand = await asyncio.to_thread(self.router.chat, self._window(), tools)
            except NoModelAvailable as exc:
                self.on_event(Event("error", str(exc)))
                return str(exc)
            calls = result.tool_calls or rescue_tool_calls(result.content, self.toolbox.tool_names)
            if not calls:
                reply = result.content.strip() or "(no answer)"
                self.history.append({"role": "assistant", "content": reply})
                self.on_event(Event("reply", reply))
                return reply
            calls = [{"id": c.get("id") or f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                      "function": {"name": c["function"]["name"],
                                   "arguments": c["function"].get("arguments") if isinstance(
                                       c["function"].get("arguments"), str)
                                   else json.dumps(c["function"].get("arguments") or {})}} for c in calls]
            self.history.append({"role": "assistant", "content": result.content or None, "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                args = parse_args(call["function"]["arguments"])
                self.on_event(Event("tool", name))
                output = await self.toolbox.call(name, args)
                self.on_event(Event("result", summarize_result(output)))
                self.history.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": output[:TOOL_RESULT_CHARS]})
        reply = (f"I stopped after {self.max_steps} steps without finishing. Tell me how to continue, "
                 "or ask me to check the part with get_model_summary.")
        self.history.append({"role": "assistant", "content": reply})
        self.on_event(Event("reply", reply))
        return reply
