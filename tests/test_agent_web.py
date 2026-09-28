from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import pytest
from starlette.testclient import TestClient

from sw_agent.assistant import Toolbox
from sw_agent.config import ModelEntry, UserConfig
from sw_agent.llm import ChatResult
from sw_agent.registry import load_providers
from sw_agent.router import Router
from sw_agent.web import create_app
from sw_mcp.core import connection
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import modeling as m

PLAN = {"steps": [{"op": "box", "x": [-20, 20], "y": [0, 5], "z": [-10, 10]}]}


class Scripted:
    def __init__(self):
        self.calls = 0

    def chat(self, model, messages, tools=None, tool_choice=None, max_tokens=None, temperature=0.1):
        self.calls += 1
        if messages[-1]["role"] == "tool":
            return ChatResult("Built a **40 x 5 x 20** plate.", [], {}, {}, 5)
        call = {"id": f"c{self.calls}", "type": "function",
                "function": {"name": "build_part", "arguments": json.dumps({"plan": json.dumps(PLAN)})}}
        return ChatResult("", [call], {}, {}, 5)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(m, "_last_group", {})
    app_fake = make_modeling_app()
    connection.use_app_factory(lambda: app_fake)
    cfg = UserConfig()
    cfg.record("groq", [ModelEntry("m1", True, 100), ModelEntry("m2", True, 900)])

    @asynccontextmanager
    async def backend():
        router = Router(cfg, providers=load_providers(), key_for=lambda p: "k",
                        client_factory=lambda p, k: Scripted())
        async with Toolbox() as toolbox:
            yield router, toolbox

    with TestClient(create_app(backend, token="secret-token")) as c:
        c.fake = app_fake
        yield c
    connection.use_app_factory(None)


H = {"X-Token": "secret-token"}


def events_until_idle(client, timeout=10.0):
    got, after, deadline = [], 0, time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/events?after={after}&wait=0", headers=H).json()
        for e in data["events"]:
            after = max(after, e["id"])
            got.append(e)
        if any(e["kind"] == "idle" for e in got):
            return got
        time.sleep(0.05)
    raise AssertionError(f"no idle event: {got}")


def test_page_has_the_token_and_api_needs_it(client):
    page = client.get("/").text
    assert "secret-token" in page and "__TOKEN__" not in page
    assert client.get("/api/status").status_code == 403
    assert client.post("/api/send", json={"text": "hi"}, headers={"X-Token": "wrong"}).status_code == 403


def test_send_shows_progress_and_reply(client):
    assert client.post("/api/send", json={"text": "a small plate"}, headers=H).json() == {"ok": True}
    events = events_until_idle(client)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "user" and "tool" in kinds and "result" in kinds and "reply" in kinds
    assert next(e for e in events if e["kind"] == "tool")["text"] == "build_part"
    assert "40 x 5 x 20" in next(e for e in events if e["kind"] == "reply")["text"]
    assert len(client.fake.created) == 1  # a real (fake) part was built


def test_empty_message_rejected(client):
    assert client.post("/api/send", json={"text": "  "}, headers=H).status_code == 400


def test_model_controls(client):
    models = client.get("/api/status", headers=H).json()["models"]
    assert [m["model"] for m in models] == ["m1", "m2"]
    assert client.post("/api/control", json={"action": "use", "n": 2}, headers=H).json()["ok"]
    status = client.get("/api/status", headers=H).json()
    assert status["pinned"] and status["models"][0]["model"] == "m2"
    client.post("/api/control", json={"action": "auto"}, headers=H)
    assert client.get("/api/status", headers=H).json()["models"][0]["model"] == "m1"
    assert client.post("/api/control", json={"action": "use", "n": 9}, headers=H).status_code == 400
    assert client.post("/api/control", json={"action": "new"}, headers=H).json()["ok"]
