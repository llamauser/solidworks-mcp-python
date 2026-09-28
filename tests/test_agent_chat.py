from __future__ import annotations

import asyncio
import json

import pytest

from sw_agent.assistant import Assistant, Toolbox, parse_args, rescue_tool_calls, summarize_result
from sw_agent.config import ModelEntry, UserConfig
from sw_agent.llm import ChatResult, LLMError
from sw_agent.registry import load_providers
from sw_agent.router import NoModelAvailable, Router
from sw_mcp.core import connection
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import modeling as m

PLATE_PLAN = {"steps": [
    {"op": "box", "x": [-30, 30], "y": [0, 10], "z": [-20, 20]},
    {"op": "cylinder", "mode": "cut", "start": [-22, -1, -12], "end": [-22, 11, -12], "diameter": 6},
    {"op": "repeat", "copies": 1, "step": [44, 0, 0]},
    {"op": "repeat", "copies": 1, "step": [0, 0, 24]},
], "expect": {"size": [60, 10, 40]}}


def text(content: str) -> ChatResult:
    return ChatResult(content, [], {}, {}, 10)


def tool_call(name: str, args: dict, cid: str = "c1") -> ChatResult:
    return ChatResult("", [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}],
                      {}, {}, 10)


class Scripted:
    """A fake provider client: returns (or raises) scripted answers, records requests."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def chat(self, model, messages, tools=None, tool_choice=None, max_tokens=None, temperature=0.1):
        self.requests.append({"model": model, "messages": messages, "tools": tools})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def config_with(*models) -> UserConfig:
    cfg = UserConfig()
    by_provider: dict[str, list] = {}
    for pid, mid, score, latency in models:
        by_provider.setdefault(pid, []).append(ModelEntry(mid, True, latency, score))
    for pid, entries in by_provider.items():
        cfg.record(pid, entries)
    return cfg


def make_router(cfg, scripts: dict[str, Scripted], clock=None):
    events = []
    router = Router(cfg, providers=load_providers(), key_for=lambda pid: "k",
                    client_factory=lambda p, k: scripts[p.id], on_event=events.append,
                    clock=clock or (lambda: 0.0))
    return router, events


# ---------------------------------------------------------------- router
def test_router_orders_by_score_then_speed():
    cfg = config_with(("groq", "fast", None, 200), ("gemini", "smart", 90.0, 2000), ("groq", "slow", None, 5000))
    router, _ = make_router(cfg, {})
    assert [c.model for c in router.candidates()] == ["smart", "fast", "slow"]
    assert [c.model for c in router.candidates("fast")] == ["fast", "smart", "slow"]
    router.pin("groq", "slow")
    assert router.candidates()[0].model == "slow"


def test_router_falls_back_on_rate_limit_and_pauses():
    now = [0.0]
    groq = Scripted([LLMError("rate_limit", "429", 429, retry_after=20), text("from groq later")])
    gemini = Scripted([text("from gemini"), text("gemini again")])
    cfg = config_with(("groq", "g", 90.0, 100), ("gemini", "m", 50.0, 100))
    router, events = make_router(cfg, {"groq": groq, "gemini": gemini}, clock=lambda: now[0])
    res, cand = router.chat([{"role": "user", "content": "hi"}])
    assert res.content == "from gemini" and cand.provider.id == "gemini"
    assert any("rate-limited" in e for e in events)
    res, _ = router.chat([{"role": "user", "content": "hi"}])  # groq still paused
    assert res.content == "gemini again"
    now[0] = 25.0  # pause over
    res, cand = router.chat([{"role": "user", "content": "hi"}])
    assert res.content == "from groq later" and cand.provider.id == "groq"


def test_router_disables_refused_key_and_reports_when_nothing_left():
    groq = Scripted([LLMError("auth", "401", 401)])
    router, events = make_router(config_with(("groq", "g", None, 100)), {"groq": groq})
    with pytest.raises(NoModelAvailable):
        router.chat([{"role": "user", "content": "x"}])
    assert router.candidates() == [] and any("refused" in e for e in events)
    with pytest.raises(NoModelAvailable) as info:
        make_router(UserConfig(), {})[0].chat([])
    assert "sw-agent setup" in str(info.value)


def test_router_busy_message_gives_wait_time():
    groq = Scripted([LLMError("rate_limit", "429", 429, retry_after=40)])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": groq})
    with pytest.raises(NoModelAvailable):
        router.chat([])
    with pytest.raises(NoModelAvailable) as info:
        router.chat([])
    assert "about 40 s" in str(info.value)


# ---------------------------------------------------------------- rescue + helpers
def test_rescue_plan_written_as_text():
    content = "Here is my plan:\n```json\n" + json.dumps(PLATE_PLAN) + "\n```"
    calls = rescue_tool_calls(content, {"build_part"})
    assert len(calls) == 1 and calls[0]["function"]["name"] == "build_part"
    assert json.loads(json.loads(calls[0]["function"]["arguments"])["plan"]) == PLATE_PLAN


def test_rescue_hermes_style_tool_call():
    content = 'Sure. <tool_call>{"name": "get_status", "arguments": {}}</tool_call>'
    calls = rescue_tool_calls(content, {"get_status"})
    assert calls[0]["function"]["name"] == "get_status"
    assert rescue_tool_calls("The part is 60 mm wide.", {"get_status"}) == []
    assert rescue_tool_calls('{"name": "rm_rf", "arguments": {}}', {"get_status"}) == []


def test_parse_args_and_summary():
    assert parse_args('{"a": 1}') == {"a": 1} and parse_args("nonsense") == {} and parse_args({"b": 2}) == {"b": 2}
    assert summarize_result('{"ok": false, "message": "No document"}').startswith("problem")
    assert "size_mm [60, 10, 40]" in summarize_result('{"ok": true, "size_mm": [60, 10, 40]}')


# ---------------------------------------------------------------- full loop against the fake SolidWorks
@pytest.fixture
def fake_solidworks(monkeypatch):
    monkeypatch.setattr(m, "_last_group", {})
    app = make_modeling_app()
    connection.use_app_factory(lambda: app)
    yield app
    connection.use_app_factory(None)


def run(coro):
    return asyncio.run(coro)


def test_assistant_builds_a_part_with_one_plan(fake_solidworks):
    llm = Scripted([
        tool_call("build_part", {"plan": json.dumps(PLATE_PLAN)}),
        text("Built a 60 x 10 x 40 mm plate with 4 holes."),
    ])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})
    events = []

    async def go():
        async with Toolbox() as toolbox:
            assistant = Assistant(router, toolbox, on_event=events.append)
            return await assistant.send("plate 60x40x10 with 4 corner holes")

    reply = run(go())
    assert "60 x 10 x 40" in reply
    part = fake_solidworks.created[0]
    assert sorted(f.Name for f in part.features if f.Name.startswith("Hole")) == ["Hole1", "Hole2", "Hole3", "Hole4"]
    assert [e.kind for e in events if e.kind in ("tool", "result")] == ["tool", "result"]
    # the second request carried the tool result back to the model, linked by id
    second = llm.requests[1]["messages"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c1" and '"ok":true' in second[-1]["content"]
    assert second[0]["role"] == "system" and "build_part" in second[0]["content"]
    assert {t["function"]["name"] for t in llm.requests[0]["tools"]} <= set(
        ("get_status", "open_document", "save_document", "build_part", "get_model_summary",
         "get_selection_context", "set_dimension", "undo_last_feature", "make_assembly", "list_project"))


def test_assistant_rescues_a_plan_sent_as_text_and_fixes_after_error(fake_solidworks):
    bad_plan = {"steps": [{"op": "box", "x": [-30, 30], "y": [0, 10], "z": [-20, 20]},
                          {"op": "cylinder", "mode": "cut", "start": [0, 0, 0], "end": [5, 10, 0], "diameter": 5}]}
    good_plan = {"steps": [bad_plan["steps"][0],
                           {"op": "cylinder", "mode": "cut", "start": [0, -1, 0], "end": [0, 11, 0], "diameter": 5}]}
    llm = Scripted([
        text("```json\n" + json.dumps(bad_plan) + "\n```"),   # plan as text, with a mistake
        tool_call("build_part", {"plan": json.dumps(good_plan)}, "c2"),  # model reads the error and fixes it
        text("Done: plate with a center hole."),
    ])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})

    async def go():
        async with Toolbox() as toolbox:
            return await Assistant(router, toolbox).send("plate with a hole")

    assert run(go()) == "Done: plate with a center hole."
    error_msg = llm.requests[1]["messages"][-1]["content"]
    assert "Step 2 (cylinder)" in error_msg and "BAD_ARGUMENT" in error_msg
    assert len(fake_solidworks.created) == 1  # the invalid plan never created a part


def test_assistant_stops_after_step_limit(fake_solidworks):
    llm = Scripted([tool_call("get_status", {}, f"c{i}") for i in range(10)])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})

    async def go():
        async with Toolbox() as toolbox:
            return await Assistant(router, toolbox, max_steps=3).send("loop forever")

    assert "stopped after 3 steps" in run(go())


def test_history_window_starts_at_a_user_message(fake_solidworks):
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": Scripted([])})
    assistant = Assistant(router, toolbox=None)  # type: ignore[arg-type]
    for i in range(30):
        assistant.history += [{"role": "user", "content": f"q{i}"},
                              {"role": "assistant", "content": None, "tool_calls": [{"id": f"t{i}"}]},
                              {"role": "tool", "tool_call_id": f"t{i}", "content": "x" * 1000}]
    window = assistant._window()
    assert window[0]["role"] == "system" and window[1]["role"] == "user"
    assert len(window) <= 25
    assert len(window[3]["content"]) < 1000  # older tool output shortened
    assert window[-1]["content"] == "x" * 1000  # the newest one kept
