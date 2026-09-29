"""The browser front end for OpenCode, against a fake OpenCode and the fake SolidWorks."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import pytest
from starlette.testclient import TestClient

from sw_agent import jobs
from sw_agent.web import create_app
from sw_mcp.core import connection
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import modeling as m
from sw_mcp.sw import plan as p
from sw_mcp.tools.plan import build_part
from tests import fake_opencode as oc

PLATE = {"steps": [{"op": "box", "x": [-20, 20], "y": [0, 5], "z": [-10, 10]}]}
H = {"X-Token": "secret-token"}


def build_script(fake, sid, text):
    """The model builds a plate (the real build_part on the fake SolidWorks) and answers."""
    args = {"plan": json.dumps(PLATE), "save_as": "Plates/plate40"}
    return oc.with_tools(sid, [("build_part", args, build_part(**args))], "Built a **40 x 5 x 20** plate.")


@pytest.fixture
def fake_sw(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_last_group", {})
    monkeypatch.setattr(p, "_unsaved_failed_builds", [])
    monkeypatch.setenv("SW_MCP_PROJECTS", str(tmp_path / "projects"))
    app = make_modeling_app()
    connection.use_app_factory(lambda: app)
    yield app
    connection.use_app_factory(None)


def make_client(fake):
    @asynccontextmanager
    async def backend():
        yield fake

    return TestClient(create_app(backend, token="secret-token"))


def events_until(client, kind="idle", timeout=15.0):
    got, after, deadline = [], 0, time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/events?after={after}&wait=0", headers=H).json()
        for e in data["events"]:
            after = max(after, e["id"])
            got.append(e)
        if any(e["kind"] == kind for e in got):
            return got
        time.sleep(0.05)
    raise AssertionError(f"no {kind} event: {[e['kind'] for e in got]}")


def test_page_has_the_token_and_the_api_needs_it():
    with make_client(oc.FakeOpenCode()) as client:
        page = client.get("/").text
        assert "secret-token" in page and "__TOKEN__" not in page
        assert client.get("/api/status").status_code == 403
        assert client.post("/api/send", json={"text": "hi"}, headers={"X-Token": "wrong"}).status_code == 403


def test_a_message_goes_to_the_solidworks_agent_and_the_answer_comes_back():
    fake = oc.FakeOpenCode()
    with make_client(fake) as client:
        assert client.post("/api/send", json={"text": "hello"}, headers=H).json()["ok"]
        events = events_until(client)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "user" and "thinking" in kinds and kinds[-1] == "idle"
    reply = next(e for e in events if e["kind"] == "reply")
    assert reply["text"] == "Hello."  # the reasoning part is not shown as the answer
    assert any(e["kind"] == "model" and "nemotron-3-ultra-free" in e["text"] for e in events)
    assert fake.prompts[0]["text"] == "hello" and fake.prompts[0]["session"] == fake.sessions[0]


def test_tool_steps_show_arguments_preview_and_result_and_the_job_is_recorded(fake_sw):
    fake = oc.FakeOpenCode(build_script)
    with make_client(fake) as client:
        client.post("/api/send", json={"text": "plate 40 x 5 x 20"}, headers=H)
        events = events_until(client)
    step = next(e for e in events if e["kind"] == "tool")
    assert step["text"] == "build_part" and json.loads(step["data"]["args"]["plan"]) == PLATE
    assert step["data"]["preview"]["steps"][0]["what"].startswith("box x -20..20")
    result = next(e for e in events if e["kind"] == "result")
    assert "40 x 5 x 20 mm" in result["text"] and '"ok":true' in result["data"]["output"]
    job = next(e for e in events if e["kind"] == "job")
    assert job["data"]["changed"] is True
    record = jobs.load(job["data"]["id"])
    assert record["request"] == "plate 40 x 5 x 20" and record["reply"] == "Built a **40 x 5 x 20** plate."
    assert record["steps"][0]["tool"] == "build_part" and record["steps"][0]["seconds"] == 2.5
    assert record["project"] == "Plates" and record["result"]["parts"]["plate40"]["size_mm"] == [40.0, 5.0, 20.0]
    assert record["picture"].startswith("picture.")


def test_the_shared_design_goes_with_the_next_message(fake_sw):
    fake = oc.FakeOpenCode(build_script)
    with make_client(fake) as client:
        client.post("/api/send", json={"text": "plate"}, headers=H)
        events_until(client)
        fake.script = lambda f, sid, text: oc.reply_only(sid, "ok")
        client.post("/api/send", json={"text": "make it thicker"}, headers=H)
        events_until(client, "idle")
    assert fake.prompts[0]["system"] == ""
    assert 'CURRENT DESIGN of project "Plates"' in fake.prompts[1]["system"]
    assert "plate40" in fake.prompts[1]["system"]


def test_a_failed_tool_is_shown_as_a_problem():
    def script(fake, sid, text):
        return oc.with_tools(sid, [("get_status", {}, json.dumps({"ok": False, "message": "SolidWorks is not running."}))],
                             "It is not running.")

    with make_client(oc.FakeOpenCode(script)) as client:
        client.post("/api/send", json={"text": "status"}, headers=H)
        events = events_until(client)
    assert next(e for e in events if e["kind"] == "result")["text"] == "problem: SolidWorks is not running."
    assert next(e for e in events if e["kind"] == "job")["data"]["changed"] is False


def test_permission_questions_are_answered_from_the_page():
    def script(fake, sid, text):
        return [oc.busy(sid), oc.permission(sid, "per_1")]

    fake = oc.FakeOpenCode(script)
    with make_client(fake) as client:
        client.post("/api/send", json={"text": "list my files"}, headers=H)
        asked = next(e for e in events_until(client, "permission") if e["kind"] == "permission")
        assert asked["data"]["id"] == "per_1" and "bash" in asked["text"]
        assert client.get("/api/status", headers=H).json()["permissions"][0]["id"] == "per_1"
        assert client.post("/api/permission", json={"id": "per_1", "reply": "once"}, headers=H).json()["ok"]
        events = events_until(client)
        assert client.post("/api/permission", json={"id": "per_1", "reply": "once"}, headers=H).status_code == 409
    assert fake.replies == [("per_1", "once", "")]
    assert any(e["kind"] == "reply" and "after your answer" in e["text"] for e in events)


def test_stop_new_conversation_and_model_choice():
    def script(fake, sid, text):
        return [oc.busy(sid)]  # keeps running until stopped

    fake = oc.FakeOpenCode(script)
    with make_client(fake) as client:
        client.post("/api/send", json={"text": "long job"}, headers=H)
        events_until(client, "thinking")
        assert client.post("/api/send", json={"text": "again"}, headers=H).status_code == 409
        assert client.post("/api/control", json={"action": "new"}, headers=H).status_code == 409  # busy
        assert client.post("/api/control", json={"action": "stop"}, headers=H).json()["ok"]
        events_until(client, "idle")
        assert fake.aborted == [fake.sessions[0]]
        assert client.post("/api/control", json={"action": "new"}, headers=H).json()["ok"]
        assert len(fake.sessions) == 2
        status = client.get("/api/status", headers=H).json()
        assert {m["modelID"] for m in status["models"]} == {"nemotron-3-ultra-free", "qwen/qwen3.8-27b:free"}
        assert client.post("/api/control", json={"action": "use", "providerID": "openrouter",
                                                 "modelID": "qwen/qwen3.8-27b:free"}, headers=H).json()["ok"]
        fake.script = lambda f, sid, text: oc.reply_only(sid, "ok")
        client.post("/api/send", json={"text": "hi"}, headers=H)
        events_until(client, "reply")
        assert fake.prompts[-1]["model"] == {"providerID": "openrouter", "modelID": "qwen/qwen3.8-27b:free"}
        assert fake.prompts[-1]["session"] == fake.sessions[1]
        client.post("/api/control", json={"action": "auto"}, headers=H)
        assert client.get("/api/status", headers=H).json()["chosen"] is None


def test_rate_and_share(fake_sw, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "pack", lambda dest=None: (tmp_path / "builds.zip", 1))
    with make_client(oc.FakeOpenCode(build_script)) as client:
        client.post("/api/send", json={"text": "plate"}, headers=H)
        job = next(e for e in events_until(client) if e["kind"] == "job")
        r = client.post("/api/rate", json={"id": job["data"]["id"], "stars": 5, "tags": ["other"], "comment": "great"},
                        headers=H)
        assert r.json()["rating"]["stars"] == 5 and jobs.load(job["data"]["id"])["rating"]["comment"] == "great"
        assert client.post("/api/rate", json={"id": "nope", "stars": 5}, headers=H).status_code == 400
        shared = client.post("/api/share?open=0", json={}, headers=H).json()
        assert shared["ok"] and shared["jobs"] == 1


def test_events_of_other_sessions_are_ignored():
    def script(fake, sid, text):
        return oc.reply_only("ses_other", "not for you") + oc.reply_only(sid, "for you")

    with make_client(oc.FakeOpenCode(script)) as client:
        client.post("/api/send", json={"text": "hi"}, headers=H)
        events = events_until(client)
    assert [e["text"] for e in events if e["kind"] == "reply"] == ["for you"]
