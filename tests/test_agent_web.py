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


def wait_for(client, kind, after=0, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in client.get(f"/api/events?after={after}&wait=0", headers=H).json()["events"]:
            if e["kind"] == kind:
                return e
        time.sleep(0.05)
    raise AssertionError(f"no {kind} event")


def test_review_mode_shows_the_plan_and_runs_the_edited_version(client):
    assert client.post("/api/control", json={"action": "review", "mode": "builds"}, headers=H).json()["ok"]
    assert client.post("/api/send", json={"text": "plate"}, headers=H).json()["ok"]
    review = wait_for(client, "review")
    data = review["data"]
    assert data["name"] == "build_part" and data["preview"]["steps"][0]["what"].startswith("box x -20..20")
    assert client.get("/api/status", headers=H).json()["pending"]["id"] == data["id"]
    edited = {"plan": {"steps": [{"op": "box", "x": [-25, 25], "y": [0, 5], "z": [-10, 10]}]}}
    check = client.post("/api/preview", json={"name": "build_part", "args": edited}, headers=H).json()
    assert check["ok"] and "x -25..25" in check["steps"][0]["what"]
    bad = client.post("/api/preview", json={"name": "build_part", "args": {"plan": {"steps": []}}}, headers=H).json()
    assert bad["ok"] is False
    assert client.post("/api/review", json={"id": data["id"], "action": "run", "args": edited}, headers=H).json()["ok"]
    events = events_until_idle(client)
    result = next(e for e in events if e["kind"] == "result")
    assert "50 x 5 x 20" in result["text"]  # the edited 50 mm plate was built
    assert client.post("/api/review", json={"id": data["id"], "action": "run"}, headers=H).status_code == 409
    ctx = client.get("/api/context", headers=H).json()
    assert ctx["review"] == "builds" and ctx["messages"][0]["role"] == "system"


def test_stop_while_waiting_for_review(client):
    client.post("/api/control", json={"action": "review", "mode": "builds"}, headers=H)
    client.post("/api/send", json={"text": "plate"}, headers=H)
    wait_for(client, "review")
    assert client.post("/api/control", json={"action": "stop"}, headers=H).json()["ok"]
    events = events_until_idle(client)
    assert any(e["kind"] == "reply" and e["text"].startswith("Stopped") for e in events)
    assert not client.fake.created
    assert client.post("/api/control", json={"action": "stop"}, headers=H).status_code == 409  # nothing running


def test_pick_any_model_and_switch_openai_extras(client, monkeypatch):
    from sw_agent.config import UserConfig

    listed = client.get("/api/providers", headers=H).json()
    assert {"groq", "openai"} <= {p["id"] for p in listed["providers"]}  # every provider with a key
    monkeypatch.setattr(Scripted, "list_models", lambda self: [{"id": "gpt-5.6-terra"}, {"id": "whisper-1"}], raising=False)
    models = client.get("/api/models?provider=openai", headers=H).json()
    assert models["ok"] and models["models"] == ["gpt-5.6-terra"]
    r = client.post("/api/control", json={"action": "pick", "provider": "openai", "model": "gpt-5.6-terra",
                                          "only": True}, headers=H)
    assert r.json()["ok"]
    status = client.get("/api/status", headers=H).json()
    assert status["models"][0]["model"] == "gpt-5.6-terra" and status["models"][0]["state"] == "only this one"
    assert client.post("/api/control", json={"action": "pick", "provider": "nope", "model": "x"}, headers=H).status_code == 400
    client.post("/api/control", json={"action": "openai_tools", "internet": True, "terminal": False}, headers=H)
    saved = UserConfig.load()
    assert saved.manual["model"] == "gpt-5.6-terra" and saved.openai_tools == {"internet": True, "terminal": False}
    assert client.get("/api/providers", headers=H).json()["openai_tools"]["internet"] is True


def test_rate_the_build_and_pack_the_records(client, tmp_path, monkeypatch):
    from sw_agent import jobs

    monkeypatch.setattr(jobs, "pack", lambda dest=None: (tmp_path / "builds.zip", 1))
    client.post("/api/send", json={"text": "plate"}, headers=H)
    events = events_until_idle(client)
    job = next(e for e in events if e["kind"] == "job")
    assert job["data"]["changed"] is True
    r = client.post("/api/rate", json={"id": job["data"]["id"], "stars": 5, "tags": ["other"], "comment": "great"},
                    headers=H)
    assert r.json()["rating"]["stars"] == 5 and jobs.load(job["data"]["id"])["rating"]["comment"] == "great"
    assert client.post("/api/rate", json={"id": "nope", "stars": 5}, headers=H).status_code == 400
    shared = client.post("/api/share?open=0", json={}, headers=H).json()
    assert shared["ok"] and shared["jobs"] == 1
