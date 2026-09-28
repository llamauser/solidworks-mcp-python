"""Review mode: see the plan, edit it, skip it with a note, or stop; and the context view."""

from __future__ import annotations

import asyncio
import json

import pytest

from sw_agent.assistant import Assistant, Decision, Toolbox
from sw_agent.review import edited_args, preview_call
from sw_mcp.core import connection
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import modeling as m
from tests.test_agent_chat import Scripted, config_with, make_router, text, tool_call

PLAN = {"steps": [{"op": "box", "x": [-30, 30], "y": [0, 10], "z": [-20, 20]},
                  {"op": "cylinder", "mode": "cut", "start": [0, -1, 0], "end": [0, 11, 0], "diameter": 6}]}


@pytest.fixture
def fake_solidworks(monkeypatch):
    monkeypatch.setattr(m, "_last_group", {})
    app = make_modeling_app()
    connection.use_app_factory(lambda: app)
    yield app
    connection.use_app_factory(None)


def run(coro):
    return asyncio.run(coro)


def converse(llm, decide, review="builds", message="plate with a hole"):
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})
    seen, events = [], []

    async def approver(call):
        seen.append(call)
        return decide(call)

    async def go():
        async with Toolbox() as toolbox:
            assistant = Assistant(router, toolbox, on_event=events.append, approver=approver, review=review)
            reply = await assistant.send(message)
            return reply, assistant

    reply, assistant = run(go())
    return reply, assistant, seen, events


def test_preview_describes_a_plan_and_catches_mistakes():
    good = preview_call("build_part", {"plan": json.dumps(PLAN)})
    assert good["ok"] and [s["what"] for s in good["steps"]] == [
        "box x -30..30, y 0..10, z -20..20", "cylinder d6 from (0, -1, 0) to (0, 11, 0)"]
    bad = preview_call("build_part", {"plan": '{"steps":[{"op":"cylinder","start":[0,0,0],"end":[1,1,1],"diameter":3}]}'})
    assert bad["ok"] is False and "Step 1" in bad["message"]
    engine = preview_call("make_engine", {"layout": "v", "cylinders": 4})
    assert engine["ok"] and len(engine["parts"]) == 12 and engine["displacement_cc"] > 1000
    assert preview_call("get_status", {}) is None
    assert json.loads(edited_args({}, {"plan": PLAN})["plan"]) == PLAN  # an object plan goes back as text


def test_run_waits_for_approval_and_the_model_sees_the_edit(fake_solidworks):
    llm = Scripted([tool_call("build_part", {"plan": json.dumps(PLAN)}), text("Done.")])
    bigger = json.loads(json.dumps(PLAN))
    bigger["steps"][1]["diameter"] = 8

    reply, assistant, seen, events = converse(llm, lambda call: Decision("run", {"plan": json.dumps(bigger)}))
    assert reply == "Done." and seen[0].name == "build_part"
    part = fake_solidworks.created[0]
    assert any(abs(c["radius"] - 4) < 1e-9 for c in part.cylinders)  # the edited 8 mm hole was built
    sent = llm.requests[1]["messages"]
    call = next(msg for msg in sent if msg.get("tool_calls"))["tool_calls"][0]
    assert json.loads(json.loads(call["function"]["arguments"])["plan"])["steps"][1]["diameter"] == 8
    assert any(e.kind == "tool" and e.data and "plan" in e.data["args"] for e in events)
    assert any(e.kind == "result" and e.data and '"ok":true' in e.data["output"] for e in events)


def test_skip_with_a_note_goes_back_to_the_model(fake_solidworks):
    llm = Scripted([tool_call("build_part", {"plan": json.dumps(PLAN)}), text("OK, 8 mm then?")])
    reply, _, _, _ = converse(llm, lambda call: Decision("skip", note="make the hole 8 mm"))
    assert reply == "OK, 8 mm then?" and not fake_solidworks.created  # nothing was built
    result = llm.requests[1]["messages"][-1]["content"]
    assert "USER_SKIPPED" in result and "make the hole 8 mm" in result


def test_stop_ends_the_request_and_answers_every_call(fake_solidworks):
    llm = Scripted([tool_call("build_part", {"plan": json.dumps(PLAN)})])
    reply, assistant, _, _ = converse(llm, lambda call: Decision("stop"))
    assert reply.startswith("Stopped") and not fake_solidworks.created and len(llm.requests) == 1
    kinds = [msg["role"] for msg in assistant.history]
    assert kinds == ["user", "assistant", "tool", "assistant"]  # the call got an answer, so history stays valid
    assert "USER_STOPPED" in assistant.history[2]["content"]


def test_review_off_and_tools_outside_the_set_run_without_asking(fake_solidworks):
    llm = Scripted([tool_call("get_status", {}, "s1"), tool_call("build_part", {"plan": json.dumps(PLAN)}, "b1"),
                    text("Built.")])
    reply, _, seen, _ = converse(llm, lambda call: Decision("stop"), review="off")
    assert reply == "Built." and not seen
    llm = Scripted([tool_call("get_status", {}, "s1"), text("Ready.")])
    _, _, seen, _ = converse(llm, lambda call: Decision("stop"), review="builds")
    assert not seen  # get_status changes nothing, so it never waits


def test_stop_request_is_honoured_before_the_next_step(fake_solidworks):
    llm = Scripted([tool_call("get_status", {}, "s1"), tool_call("get_status", {}, "s2"), text("never")])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})

    async def go():
        async with Toolbox() as toolbox:
            assistant = Assistant(router, toolbox)
            assistant.on_event = lambda e: assistant.request_stop() if e.kind == "result" else None
            return await assistant.send("status")

    assert run(go()).startswith("Stopped") and len(llm.requests) == 1


def test_context_shows_what_the_model_gets(fake_solidworks):
    llm = Scripted([tool_call("build_part", {"plan": json.dumps(PLAN)}), text("Done.")])
    _, assistant, _, _ = converse(llm, lambda call: Decision("run", call.args))
    ctx = assistant.context()
    assert ctx["messages"][0]["role"] == "system" and ctx["estimated_tokens"] > 1000
    assert "build_part" in ctx["tools"] and ctx["review"] == "builds" and ctx["model"].startswith("Groq")
    assert any(msg.get("content") == "plate with a hole" for msg in ctx["messages"])
