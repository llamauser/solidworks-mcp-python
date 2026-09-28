"""OpenAI as a provider, choosing any model by hand, and the OpenAI-only extras."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from sw_agent import extras
from sw_agent.assistant import Assistant, Decision, Toolbox
from sw_agent.config import UserConfig
from sw_agent.llm import ChatClient, LLMError
from sw_agent.probe import rank_models
from sw_agent.registry import load_providers
from sw_mcp.core import connection
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import modeling as m
from tests.test_agent_chat import Scripted, config_with, make_router, text, tool_call

OPENAI = next(p for p in load_providers() if p.id == "openai")


@pytest.fixture
def fake_solidworks(monkeypatch):
    monkeypatch.setattr(m, "_last_group", {})
    app = make_modeling_app()
    connection.use_app_factory(lambda: app)
    yield app
    connection.use_app_factory(None)


def run(coro):
    return asyncio.run(coro)


RESPONSE_WITH_CALL = {"status": "completed", "usage": {"input_tokens": 1200, "output_tokens": 80}, "output": [
    {"type": "reasoning", "id": "rs_1", "summary": []},
    {"type": "message", "content": [{"type": "output_text", "text": "Building it."}]},
    {"type": "function_call", "id": "fc_1", "call_id": "call_abc", "name": "build_part",
     "arguments": "{\"plan\": \"{}\"}"}]}


def openai_call(model: str, messages=None, tools=None, answer=None):
    sent = []

    def handler(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=answer or {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": "hi"}]}]})

    client = ChatClient(OPENAI, "k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.chat(model, messages or [{"role": "user", "content": "x"}], tools=tools, max_tokens=300)
    return sent[0][0], sent[0][1], result


# ---------------------------------------------------------------- provider
def test_openai_is_a_paid_provider_with_its_key_page():
    assert OPENAI.paid and OPENAI.base_url == "https://api.openai.com/v1"
    assert OPENAI.key_url.startswith("https://platform.openai.com/")


def test_openai_uses_the_responses_api_with_its_own_parameters():
    """Log 2026-09-28 15:29: gpt-5.6 models refuse function tools on /chat/completions."""
    path, new, _ = openai_call("gpt-5.6-terra")
    assert path == "/v1/responses" and "temperature" not in new and new["max_output_tokens"] >= 8000
    assert new["store"] is False
    _, old, _ = openai_call("gpt-4.1-mini")
    assert old["temperature"] == 0.1 and old["max_output_tokens"] == 300


def test_conversation_and_tools_are_translated_both_ways():
    tool = {"type": "function", "function": {"name": "build_part", "description": "Build.",
                                             "parameters": {"type": "object", "properties": {"plan": {"type": "string"}}}}}
    history = [{"role": "system", "content": "You are a CAD operator."},
               {"role": "user", "content": "plate"},
               {"role": "assistant", "content": None, "tool_calls": [
                   {"id": "call_1", "type": "function", "function": {"name": "get_status", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\":true}"},
               {"role": "assistant", "content": "Ready."},
               {"role": "user", "content": "go"}]
    _, sent, result = openai_call("gpt-5-mini", history, [tool], RESPONSE_WITH_CALL)
    assert sent["instructions"] == "You are a CAD operator."
    assert sent["input"] == [
        {"role": "user", "content": "plate"},
        {"type": "function_call", "call_id": "call_1", "name": "get_status", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "{\"ok\":true}"},
        {"role": "assistant", "content": "Ready."},
        {"role": "user", "content": "go"}]
    assert sent["tools"] == [{"type": "function", "name": "build_part", "description": "Build.",
                              "parameters": tool["function"]["parameters"], "strict": False}]
    assert result.content == "Building it." and result.usage == {"prompt_tokens": 1200, "completion_tokens": 80}
    assert result.tool_calls == [{"id": "call_abc", "type": "function",
                                  "function": {"name": "build_part", "arguments": "{\"plan\": \"{}\"}"}}]


def test_an_empty_openai_answer_is_an_error():
    with pytest.raises(LLMError) as info:
        openai_call("gpt-5-mini", answer={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                                          "output": [{"type": "reasoning", "summary": []}]})
    assert info.value.kind == "bad_output" and "max_output_tokens" in str(info.value)


def test_no_credit_is_explained_and_the_provider_is_set_aside():
    from sw_agent.llm import _classify
    from sw_agent.router import NoModelAvailable

    err = _classify(httpx.Response(429, json={"error": {"message": "You have no credits remaining. Add credits to "
                                                                   "continue using the API."}}))
    assert err.kind == "no_credit"
    openai = Scripted([err])
    groq = Scripted([text("from groq")])
    router, events = make_router(config_with(("groq", "g", None, 100)), {"openai": openai, "groq": groq})
    router.pin("openai", "gpt-5")
    assert router.chat([{"role": "user", "content": "hi"}])[0].content == "from groq"
    assert any("no credit" in e and "Billing" in e for e in events)
    assert all(c.provider.id != "openai" for c in router.candidates())
    router2, _ = make_router(config_with(), {"openai": Scripted([LLMError("bad_request", "Model refused tools", 400)])})
    router2.pin("openai", "gpt-5.6-sol", only=True)
    with pytest.raises(NoModelAvailable) as info:
        router2.chat([{"role": "user", "content": "hi"}])
    assert "The model you chose did not answer" in str(info.value) and "Model refused tools" in str(info.value)


def test_openai_model_list_keeps_chat_models_only():
    ids = ["gpt-5-mini", "gpt-5-mini-2025-08-07", "gpt-4o-realtime-preview", "gpt-4o-mini-tts", "text-embedding-3-large",
           "dall-e-3", "gpt-4.1", "whisper-1", "gpt-5", "o4-mini", "gpt-4o-search-preview", "davinci-002"]
    ranked = rank_models(OPENAI, [{"id": i} for i in ids])
    assert ranked[:2] == ["gpt-5-mini", "gpt-5"] and set(ranked) == {"gpt-5-mini", "gpt-5", "gpt-4.1", "o4-mini"}


# ---------------------------------------------------------------- choosing a model by hand
def test_router_puts_paid_models_after_free_ones():
    router, _ = make_router(config_with(("openai", "gpt-5-mini", None, 50), ("groq", "g", None, 900)), {})
    assert [c.provider.id for c in router.candidates()] == ["groq", "openai"]


def test_any_model_can_be_picked_by_hand_and_is_remembered():
    cfg = config_with(("groq", "g", None, 100))
    router, _ = make_router(cfg, {})
    router.pin("openai", "gpt-5.6-terra", remember=True)  # never tested by the setup
    assert [(c.provider.id, c.model) for c in router.candidates()][0] == ("openai", "gpt-5.6-terra")
    assert len(router.candidates()) == 2  # groq stays as a fallback
    router.pin("openai", "gpt-5.6-terra", only=True, remember=True)
    assert [c.model for c in router.candidates()] == ["gpt-5.6-terra"]
    assert router.status()[0][2] == "only this one"
    saved = UserConfig.load()
    assert saved.manual == {"provider": "openai", "model": "gpt-5.6-terra", "only": True}
    again, _ = make_router(saved, {})  # a new start uses the saved choice
    assert again.pinned == ("openai", "gpt-5.6-terra") and again.only
    again.unpin(remember=True)
    assert UserConfig.load().manual == {} and [c.model for c in again.candidates()] == ["g"]


def test_picked_model_answers_the_request():
    openai = Scripted([text("from gpt-5.6-terra")])
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"openai": openai, "groq": Scripted([])})
    router.pin("openai", "gpt-5.6-terra", only=True)
    result, cand = router.chat([{"role": "user", "content": "hi"}])
    assert result.content == "from gpt-5.6-terra" and openai.requests[0]["model"] == "gpt-5.6-terra"


# ---------------------------------------------------------------- OpenAI-only extras
def assistant_with(fake_solidworks, scripts, settings, approver=None, only="openai"):
    cfg = config_with(("groq", "g", None, 100))
    cfg.openai_tools = settings
    router, _ = make_router(cfg, scripts)
    if only == "openai":
        router.pin("openai", "gpt-5-mini", only=True)
    return router, approver


def test_extras_are_offered_to_openai_only(fake_solidworks):
    openai = Scripted([text("ok")])
    groq = Scripted([text("ok")])
    for pick, llm in (("openai", openai), ("groq", groq)):
        cfg = config_with(("groq", "g", None, 100))
        cfg.openai_tools = {"internet": True, "terminal": True}
        router, _ = make_router(cfg, {"openai": openai, "groq": groq})
        router.pin(pick, "gpt-5-mini" if pick == "openai" else "g", only=True)

        async def go(router=router):
            async with Toolbox() as toolbox:
                return await Assistant(router, toolbox).send("hi")

        run(go())
    offered = {t["function"]["name"] for t in openai.requests[0]["tools"]}
    assert {"web_search", "fetch_page", "run_command"} <= offered
    assert not {"web_search", "fetch_page", "run_command"} & {t["function"]["name"] for t in groq.requests[0]["tools"]}


def test_switched_off_extras_are_not_offered(fake_solidworks):
    openai = Scripted([text("ok")])
    cfg = config_with()
    cfg.openai_tools = {"internet": True, "terminal": False}
    router, _ = make_router(cfg, {"openai": openai})
    router.pin("openai", "gpt-5-mini", only=True)

    async def go():
        async with Toolbox() as toolbox:
            return await Assistant(router, toolbox).send("hi")

    run(go())
    offered = {t["function"]["name"] for t in openai.requests[0]["tools"]}
    assert "web_search" in offered and "run_command" not in offered


def command_session(fake_solidworks, monkeypatch, decide, approver=True, provider="openai"):
    ran = []
    monkeypatch.setattr(extras, "run_command", lambda cmd, timeout=60: (ran.append(cmd), json.dumps(
        {"ok": True, "exit_code": 0, "output": "done"}))[1])
    llm = Scripted([tool_call("run_command", {"command": "Get-ChildItem"}, "r1"), text("Listed.")])
    cfg = config_with(("groq", "g", None, 100))
    cfg.openai_tools = {"internet": False, "terminal": True}
    router, _ = make_router(cfg, {provider: llm})
    router.pin(provider, "gpt-5-mini" if provider == "openai" else "g", only=True)
    seen = []

    async def approve(call):
        seen.append(call)
        return decide(call)

    async def go():
        async with Toolbox() as toolbox:
            return await Assistant(router, toolbox, approver=approve if approver else None, review="off").send("list files")

    run(go())
    return ran, seen, llm


def test_every_command_waits_for_approval_even_with_review_off(fake_solidworks, monkeypatch):
    ran, seen, llm = command_session(fake_solidworks, monkeypatch, lambda call: Decision("run", call.args))
    assert seen[0].name == "run_command" and ran == ["Get-ChildItem"]
    ran, seen, llm = command_session(fake_solidworks, monkeypatch, lambda call: Decision("skip", note="no"))
    assert ran == [] and "USER_SKIPPED" in llm.requests[1]["messages"][-1]["content"]
    ran, seen, llm = command_session(fake_solidworks, monkeypatch, None, approver=False)
    assert ran == [] and "NOT_ALLOWED" in llm.requests[1]["messages"][-1]["content"]


def test_other_models_cannot_run_commands_even_if_they_ask(fake_solidworks, monkeypatch):
    ran, seen, llm = command_session(fake_solidworks, monkeypatch, lambda call: Decision("run", call.args),
                                     provider="groq")
    assert ran == [] and "NOT_ALLOWED" in llm.requests[1]["messages"][-1]["content"]


def test_web_search_uses_openai_hosted_search_and_falls_back_to_the_preview_name():
    calls = []

    class Fake:
        def _request(self, method, path, json=None):
            calls.append(json["tools"][0]["type"])
            if json["tools"][0]["type"] == "web_search":
                raise LLMError("bad_request", "unknown tool", 400)
            return {"output": [{"type": "web_search_call"}, {"type": "message", "content": [{
                "type": "output_text", "text": "An M8 bolt has a 13 mm hex.",
                "annotations": [{"type": "url_citation", "url": "https://example.com/m8", "title": "M8"}]}]}]}

    out = json.loads(extras.web_search(Fake(), "gpt-5-mini", "M8 hex size?"))
    assert out["ok"] and "13 mm" in out["answer"] and out["sources"][0]["url"] == "https://example.com/m8"
    assert calls == ["web_search", "web_search_preview"]


def test_fetch_page_reads_text_and_refuses_other_schemes():
    page = "<html><head><style>x{}</style><script>evil()</script></head><body><h1>Bolt</h1><p>M8 &amp; M10</p></body></html>"
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})))
    out = json.loads(extras.fetch_page("https://example.com", http=http))
    assert out["ok"] and out["text"] == "Bolt\nM8 & M10"
    assert json.loads(extras.fetch_page("file:///C:/secret.txt"))["error"] == "BAD_ARGUMENT"


def test_run_command_uses_powershell_in_the_projects_folder_without_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("SW_MCP_PROJECTS", str(tmp_path))
    monkeypatch.setenv("SW_AGENT_KEY_OPENAI", "sk-secret")
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "a.SLDPRT\n", ""

    def runner(argv, **kw):
        seen.update(argv=argv, **kw)
        return Done()

    out = json.loads(extras.run_command("Get-ChildItem", 30, runner=runner))
    assert out["ok"] and out["output"] == "a.SLDPRT"
    assert seen["argv"][0] == "powershell.exe" and seen["argv"][-1].endswith("Get-ChildItem")
    assert seen["cwd"] == str(tmp_path) and seen["timeout"] == 30
    assert "SW_AGENT_KEY_OPENAI" not in seen["env"]
