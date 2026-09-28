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
    now[0] = 25.0  # pause over, but within one request the router sticks to the model that works
    router.new_turn()  # the next user request starts again from the best model
    res, cand = router.chat([{"role": "user", "content": "hi"}])
    assert res.content == "from groq later" and cand.provider.id == "groq"


def test_router_sticks_to_the_working_model_within_a_request():
    now = [0.0]
    groq = Scripted([LLMError("rate_limit", "429", 429, retry_after=2), text("groq")])
    gemini = Scripted([text("g1"), text("g2")])
    router, _ = make_router(config_with(("groq", "g", 90.0, 100), ("gemini", "m", 50.0, 100)),
                            {"groq": groq, "gemini": gemini}, clock=lambda: now[0])
    assert router.chat([{"role": "user", "content": "a"}])[0].content == "g1"
    now[0] = 10.0  # groq would be free again, but switching back and forth wastes both limits
    assert router.chat([{"role": "user", "content": "b"}])[0].content == "g2"


def test_router_skips_models_whose_token_cap_is_too_small():
    big = [{"role": "user", "content": "x" * 40000}]  # about 12500 tokens
    groq = Scripted([LLMError("too_large", "413", 413, token_limit=8000)])
    gemini = Scripted([text("fits"), text("fits again")])
    router, events = make_router(config_with(("groq", "g", 90.0, 100), ("gemini", "m", 50.0, 100)),
                                 {"groq": groq, "gemini": gemini})
    assert router.chat(big)[0].content == "fits" and any("8000 tokens" in e for e in events)
    router.new_turn()
    assert router.chat(big)[0].content == "fits again" and len(groq.requests) == 1  # not asked again
    router2, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": Scripted([
        LLMError("too_large", "413", 413, token_limit=8000)])})
    with pytest.raises(NoModelAvailable) as info:
        router2.chat(big)
    with pytest.raises(NoModelAvailable) as info:
        router2.chat(big)
    assert "too long" in str(info.value)


def test_llm_errors_are_sorted():
    import httpx
    from sw_agent.llm import _classify, with_thought_signatures

    def err(status, message, headers=None):
        return _classify(httpx.Response(status, json={"error": {"message": message}}, headers=headers or {}))

    e = err(413, "Request too large for model `x` on tokens per minute (TPM): Limit 8000, Requested 10197")
    assert e.kind == "too_large" and e.token_limit == 8000
    e = err(429, "Rate limit reached on input tokens per minute (ITPM): Limit 7000, Used 4639, Requested 4830. "
                 "Please try again in 21.16s.")
    assert e.kind == "rate_limit" and e.token_limit == 7000 and e.retry_after == pytest.approx(21.16)
    assert err(400, "Failed to call a function. Please adjust your prompt.").kind == "bad_output"
    msgs = with_thought_signatures([{"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "f", "arguments": "{}"},
         "extra_content": {"google": {"thought_signature": "real"}}}]}])
    sigs = [c["extra_content"]["google"]["thought_signature"] for c in msgs[0]["tool_calls"]]
    assert sigs == ["skip_thought_signature_validator", "real"]


def test_requests_always_set_max_tokens():
    import httpx
    from sw_agent.llm import ChatClient
    from sw_agent.registry import load_providers as lp

    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    gemini = next(p for p in lp() if p.id == "gemini")
    client = ChatClient(gemini, "k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.chat("m", [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}])
    assert sent[0]["max_tokens"] == 4096
    assert sent[0]["messages"][0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"]


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
    assert "60 x 10 x 40 mm" in summarize_result('{"ok": true, "size_mm": [60, 10, 40]}')


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
         "get_selection_context", "set_dimension", "make_assembly", "list_project",
         "manage_documents", "make_engine", "connect_parts", "move_mechanism", "make_motion_study"))


def test_assistant_rescues_a_plan_sent_as_text_and_fixes_after_error(fake_solidworks):
    bad_plan = {"steps": [{"op": "box", "x": [-30, 30], "y": [0, 10], "z": [-20, 20]},
                          {"op": "cylinder", "mode": "cut", "start": [0, 0, 0], "end": [5, 10, 5], "diameter": 5}]}
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

    assert "paused after 3 steps" in run(go())


def test_history_window_keeps_the_task_and_shrinks_the_rest(fake_solidworks):
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": Scripted([])})
    assistant = Assistant(router, toolbox=None)  # type: ignore[arg-type]
    for i in range(10):  # ten earlier requests, each with one tool call
        assistant.history += [{"role": "user", "content": f"q{i}"},
                              {"role": "assistant", "content": None, "tool_calls": [
                                  {"id": f"t{i}", "type": "function",
                                   "function": {"name": "build_part", "arguments": json.dumps({"plan": "p" * 900})}}]},
                              {"role": "tool", "tool_call_id": f"t{i}", "content": json.dumps({"ok": True, "part": f"P{i}"})},
                              {"role": "assistant", "content": f"answer {i}"}]
    task = "make a V4 engine and animate it"
    assistant.history.append({"role": "user", "content": task})
    for k in range(6):  # a long current request: six tool exchanges
        assistant.history += [{"role": "assistant", "content": None, "tool_calls": [
                                  {"id": f"c{k}", "type": "function",
                                   "function": {"name": "build_part", "arguments": json.dumps({"plan": "s" * 900})}}]},
                              {"role": "tool", "tool_call_id": f"c{k}", "content": "x" * 3000}]
    window = assistant._window()
    assert window[0]["role"] == "system"
    earlier = [m["content"] for m in window[1:] if m["role"] == "user"]
    assert earlier[-1] == task and "q9" in earlier and "q0" not in earlier  # only recent requests, task kept
    notes = [m["content"] for m in window if m["role"] == "assistant" and m.get("content")]
    assert any("build_part: done: P9" in n and "answer 9" in n for n in notes)
    tools = [m for m in window if m["role"] == "tool"]
    assert [len(t["content"]) for t in tools][-2:] == [3000, 3000]  # the newest two in full
    assert all(len(t["content"]) < 300 for t in tools[:-2])
    calls = [m for m in window if m.get("tool_calls")]
    assert "sent earlier" in calls[0]["tool_calls"][0]["function"]["arguments"]
    assert calls[-1]["tool_calls"][0]["function"]["arguments"] == json.dumps({"plan": "s" * 900})
    assert len(json.dumps(window)) < 16000


def test_continue_after_the_step_limit_still_knows_the_task(fake_solidworks):
    llm = Scripted([tool_call("get_status", {}, "a1"), tool_call("get_status", {}, "a2"), text("Carrying on.")])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})

    async def go():
        async with Toolbox() as toolbox:
            assistant = Assistant(router, toolbox, max_steps=2)
            await assistant.send("build the gearbox housing")
            return await assistant.send("continue")

    assert run(go()) == "Carrying on."
    last = llm.requests[-1]["messages"]
    assert any(m["role"] == "user" and m["content"] == "build the gearbox housing" for m in last)


def test_an_identical_failed_call_is_not_run_again(fake_solidworks):
    bad = {"file_path": "C:/nowhere/missing.SLDPRT"}
    llm = Scripted([tool_call("open_document", bad, "o1"), tool_call("open_document", bad, "o2"), text("It is missing.")])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})

    async def go():
        async with Toolbox() as toolbox:
            return await Assistant(router, toolbox).send("open missing")

    assert run(go()) == "It is missing."
    second_result = llm.requests[2]["messages"][-1]["content"]
    assert "REPEATED_CALL" in second_result and "File not found" in second_result


def test_tool_definitions_are_compact(fake_solidworks):
    async def go():
        async with Toolbox() as toolbox:
            return toolbox.tools

    tools = run(go())
    assert len(json.dumps(tools)) < 9500
    build = next(t for t in tools if t["function"]["name"] == "build_part")["function"]["description"]
    assert '"op":"cylinder"' in build and "Example" not in build


def test_assistant_makes_and_turns_a_v4_in_three_requests(fake_solidworks, monkeypatch, tmp_path):
    monkeypatch.setenv("SW_MCP_PROJECTS", str(tmp_path))
    llm = Scripted([
        tool_call("make_engine", {"project": "V4 engine", "layout": "v", "cylinders": 4}, "e1"),
        tool_call("move_mechanism", {"part": "crankshaft", "degrees": 360}, "e2"),
        text("Your V4 is built; each piston travels 70 mm."),
    ])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})
    events = []

    async def go():
        async with Toolbox() as toolbox:
            return await Assistant(router, toolbox, on_event=events.append).send(
                "make a V4 engine, turn the crankshaft and tell me how far each piston moves")

    assert "70 mm" in run(go())
    results = [e.text for e in events if e.kind == "result"]
    assert results[0].startswith("done: V4") and "13 joints" in results[0]
    assert "piston1-1 70" in results[1]
    assert max(len(json.dumps(r["messages"])) + len(json.dumps(r["tools"] or [])) for r in llm.requests) < 20000


def test_same_error_twice_stops_the_retries(fake_solidworks):
    """Log 2026-09-28 13:50: the model kept retrying make_engine with other numbers after the same error."""
    llm = Scripted([tool_call("open_document", {"file_path": f"C:/parts/v{i}.SLDPRT"}, f"o{i}") for i in range(1, 4)]
                   + [text("It keeps failing, please check the file.")])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})

    async def go():
        async with Toolbox() as toolbox:
            return await Assistant(router, toolbox).send("open my part")

    assert "keeps failing" in run(go())
    third = llm.requests[3]["messages"][-1]["content"]
    assert "STOP_RETRYING" in third and "failed twice" in third
