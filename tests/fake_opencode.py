"""A stand-in for `opencode serve`: the same client methods, and events shaped exactly like the ones
recorded from opencode 1.18 (see the probe in the development notes)."""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Callable

_ids = itertools.count(1)


def _id(prefix: str) -> str:
    return f"{prefix}_{next(_ids):04d}"


def busy(sid: str) -> dict:
    return {"type": "session.status", "properties": {"sessionID": sid, "status": {"type": "busy"}}}


def idle(sid: str) -> list[dict]:
    return [{"type": "session.status", "properties": {"sessionID": sid, "status": {"type": "idle"}}},
            {"type": "session.idle", "properties": {"sessionID": sid}}]


def message(sid: str, mid: str, role: str, model: str = "opencode/nemotron-3-ultra-free", done: bool = False) -> dict:
    provider, model_id = model.split("/", 1)
    info = {"id": mid, "role": role, "sessionID": sid, "time": {"created": 1}}
    if role == "assistant":
        info.update(providerID=provider, modelID=model_id, tokens={"input": 1200, "output": 40})
        if done:
            info["time"]["completed"] = 2
    return {"type": "message.updated", "properties": {"sessionID": sid, "info": info}}


def text(sid: str, mid: str, body: str, kind: str = "text") -> list[dict]:
    pid = _id("prt")
    return [{"type": "message.part.updated", "properties": {"sessionID": sid, "part": {
                "id": pid, "messageID": mid, "sessionID": sid, "type": kind, "text": ""}}},
            {"type": "message.part.delta", "properties": {"sessionID": sid, "messageID": mid, "partID": pid,
                                                          "field": "text", "delta": body}}]


def tool(sid: str, mid: str, name: str, args: dict, output: str | None, error: str = "") -> list[dict]:
    call = _id("call")
    pid = _id("prt")

    def part(state: dict) -> dict:
        return {"type": "message.part.updated", "properties": {"sessionID": sid, "part": {
            "id": pid, "messageID": mid, "sessionID": sid, "type": "tool", "tool": f"solidworks_{name}",
            "callID": call, "state": state}}}

    done = ({"status": "completed", "input": args, "output": output, "time": {"start": 1000, "end": 3500}}
            if not error else {"status": "error", "input": args, "error": error, "time": {"start": 1000, "end": 1200}})
    return [part({"status": "pending", "input": {}, "raw": ""}),
            part({"status": "running", "input": args, "time": {"start": 1000}}),
            part(done)]


def permission(sid: str, rid: str, what: str = "bash", patterns: list[str] | None = None) -> dict:
    return {"type": "permission.asked", "properties": {"id": rid, "sessionID": sid, "permission": what,
                                                       "patterns": patterns or ["Get-ChildItem"],
                                                       "metadata": {"command": "Get-ChildItem"}, "always": []}}


Script = Callable[["FakeOpenCode", str, str], list[dict]]


class FakeOpenCode:
    def __init__(self, script: Script | None = None) -> None:
        self.script = script or (lambda oc, sid, text_: reply_only(sid, "Hello."))
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sessions: list[str] = []
        self.prompts: list[dict] = []
        self.aborted: list[str] = []
        self.replies: list[tuple] = []

    async def create_session(self, title: str = "") -> dict:
        sid = _id("ses")
        self.sessions.append(sid)
        return {"id": sid}

    async def prompt(self, session_id: str, text_: str, model: dict | None = None, system: str = "") -> None:
        self.prompts.append({"session": session_id, "text": text_, "model": model, "system": system})
        for ev in await asyncio.to_thread(self.script, self, session_id, text_):
            await self.queue.put(ev)

    async def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)
        for ev in idle(session_id):
            await self.queue.put(ev)

    async def models(self) -> list[dict]:
        return [{"providerID": "opencode", "modelID": "nemotron-3-ultra-free", "name": "Nemotron 3 Ultra",
                 "provider": "OpenCode Zen", "default": False},
                {"providerID": "openrouter", "modelID": "qwen/qwen3.8-27b:free", "name": "Qwen", "provider": "OpenRouter",
                 "default": False}]

    async def reply_permission(self, request_id: str, reply: str, message_: str = "") -> None:
        self.replies.append((request_id, reply, message_))
        sid = self.sessions[-1]
        await self.queue.put({"type": "permission.replied", "properties": {"sessionID": sid, "requestID": request_id,
                                                                           "reply": reply}})
        for ev in reply_only(sid, "Done after your answer."):
            await self.queue.put(ev)

    async def events(self):
        while True:
            yield await self.queue.get()

    async def close(self) -> None:
        pass


def reply_only(sid: str, body: str) -> list[dict]:
    user, asst = _id("msg"), _id("msg")
    return [message(sid, user, "user"), busy(sid), message(sid, asst, "assistant"),
            *text(sid, asst, "thinking about it", kind="reasoning"), *text(sid, asst, body),
            message(sid, asst, "assistant", done=True), *idle(sid)]


def with_tools(sid: str, calls: list[tuple[str, dict, str]], final: str) -> list[dict]:
    """A request where the model calls tools (name, args, output) and then answers."""
    user, first, second = _id("msg"), _id("msg"), _id("msg")
    events = [message(sid, user, "user"), busy(sid), message(sid, first, "assistant")]
    for name, args, output in calls:
        events += tool(sid, first, name, args, output)
    events += [message(sid, first, "assistant", done=True), message(sid, second, "assistant"),
               *text(sid, second, final), message(sid, second, "assistant", done=True), *idle(sid)]
    return events


def as_json(value: dict) -> str:
    return json.dumps(value)
