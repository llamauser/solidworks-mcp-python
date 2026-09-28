from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from sw_agent import clients, keys
from sw_agent.config import ModelEntry, UserConfig
from sw_agent.llm import ChatClient, LLMError
from sw_agent.probe import Discovery, ModelCheck, discover, rank_models
from sw_agent.registry import load_providers
from sw_agent.wizard import Wizard

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- registry
def test_registry_loads_and_is_complete():
    providers = load_providers()
    ids = [p.id for p in providers]
    assert ids[0] == "openrouter" and {"gemini", "groq", "mistral", "lmstudio"} <= set(ids)
    for p in providers:
        assert p.base_url.startswith("http") and not p.base_url.endswith("/")
        assert p.privacy in {"no-training", "may-train", "depends", "local"}
        assert p.privacy_note and p.offer and p.key_url.startswith("http")


def _provider(pid: str):
    return next(p for p in load_providers() if p.id == pid)


def test_rank_models_prefers_and_filters():
    orouter = _provider("openrouter")
    models = [
        {"id": "meta/llama-3:free", "supported_parameters": ["tools"]},
        {"id": "nvidia/nemotron-3-super-120b-a12b:free", "supported_parameters": ["tools", "temperature"]},
        {"id": "some/paid-model", "supported_parameters": ["tools"]},
        {"id": "x/no-tools:free", "supported_parameters": ["temperature"]},
        {"id": "text-embed-3:free"},
    ]
    assert rank_models(orouter, models) == ["nvidia/nemotron-3-super-120b-a12b:free", "meta/llama-3:free"]
    groq = _provider("groq")
    ranked = rank_models(groq, [{"id": "whisper-large"}, {"id": "llama-3.3-70b-versatile"}, {"id": "openai/gpt-oss-120b"}])
    assert ranked == ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"]


# ---------------------------------------------------------------- HTTP client + probe
def mock_http(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def tool_call_response(name="add_numbers", args='{"a": 2, "b": 3}'):
    return {"choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": "1", "type": "function",
                                                     "function": {"name": name, "arguments": args}}]}}]}


def test_llm_error_kinds():
    groq = _provider("groq")
    for status, kind in ((401, "auth"), (429, "rate_limit"), (402, "rate_limit"), (404, "unavailable"),
                         (503, "unavailable"), (400, "bad_request")):
        client = ChatClient(groq, "k", http=mock_http(lambda r, s=status: httpx.Response(s, json={"error": {"message": "x"}})))
        with pytest.raises(LLMError) as info:
            client.list_models()
        assert info.value.kind == kind
    client = ChatClient(groq, "k", http=mock_http(lambda r: httpx.Response(200, json={"error": {"code": 429, "message": "slow down"}})))
    with pytest.raises(LLMError) as info:
        client.chat("m", [])
    assert info.value.kind == "rate_limit"


def test_discover_happy_path_and_sends_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "openai/gpt-oss-120b"}]})
        body = json.loads(request.content)
        if body["model"] == "openai/gpt-oss-120b":
            return httpx.Response(200, json=tool_call_response())
        return httpx.Response(200, json={"choices": [{"message": {"content": "5"}}]})

    found = discover(_provider("groq"), "gsk_secret", http=mock_http(handler))
    assert found.key_ok and found.model_count == 2 and seen["auth"] == "Bearer gsk_secret"
    assert [(c.model, c.tools_ok) for c in found.checks] == [("openai/gpt-oss-120b", True),
                                                             ("llama-3.3-70b-versatile", False)]
    assert "text instead" in found.checks[1].detail


def test_discover_bad_key():
    found = discover(_provider("groq"), "bad", http=mock_http(lambda r: httpx.Response(401, json={"error": {"message": "Invalid API Key"}})))
    assert not found.key_ok and found.error_kind == "auth" and "Invalid API Key" in found.error


def test_wrong_tool_arguments_do_not_pass():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-oss-120b"}]})
        return httpx.Response(200, json=tool_call_response(args='{"a": 2, "b": 4}'))

    found = discover(_provider("groq"), "k", http=mock_http(handler))
    assert found.key_ok and not found.working and "wrong arguments" in found.checks[0].detail


# ---------------------------------------------------------------- config + keys
def test_config_roundtrip(tmp_path):
    cfg = UserConfig()
    cfg.record("groq", [ModelEntry("fast", True, 300), ModelEntry("slow", True, 3000), ModelEntry("bad", False)])
    cfg.record("gemini", [ModelEntry("flash", True, 900)])
    path = cfg.save(tmp_path / "c.json")
    again = UserConfig.load(path)
    assert [(p, m.id) for p, m in again.working_models()] == [("groq", "fast"), ("gemini", "flash"), ("groq", "slow")]
    assert "secret" not in path.read_text()


@pytest.fixture
def memory_keyring(monkeypatch):
    store = {}
    monkeypatch.setattr(keys.keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keys.keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(keys.keyring, "delete_password", lambda s, u: store.pop((s, u), None))
    return store


def test_keys_env_override_and_mask(memory_keyring, monkeypatch):
    keys.set_key("groq", "  gsk_abcdef123456  ")
    assert keys.get_key("groq") == "gsk_abcdef123456"
    monkeypatch.setenv("SW_AGENT_KEY_GROQ", "from-env")
    assert keys.get_key("groq") == "from-env"
    assert keys.mask("gsk_abcdef123456") == "...3456" and keys.mask(None) == "(none)"


# ---------------------------------------------------------------- wizard
class Script:
    """Feeds scripted answers to the wizard and records the prompts."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt, password=False, default=""):
        self.prompts.append((prompt, password))
        return self.answers.pop(0)


def fake_discover(key_to_result):
    def run(provider, key, **_):
        ok = key_to_result.get(key)
        if ok is None:
            return Discovery(provider, False, "The key was refused (401): Invalid API Key", "auth")
        return Discovery(provider, True, model_count=5, checks=[ModelCheck("model-a", ok, 800, "")])
    return run


def test_wizard_sets_up_one_provider_after_a_typo(memory_keyring, tmp_path, monkeypatch):
    monkeypatch.setenv("SW_AGENT_HOME", str(tmp_path))
    providers = [_provider("groq"), _provider("gemini")]
    opened = []
    # groq: set up -> wrong key -> right key ; gemini: skip
    script = Script(["", "wrong", "gsk_good_key_1234", "s"])
    wiz = Wizard(console=Console(file=open(tmp_path / "out.txt", "w", encoding="utf-8"), width=120),
                 ask=script, open_url=opened.append, discover_fn=fake_discover({"gsk_good_key_1234": True}),
                 providers=providers, config=UserConfig())
    cfg = wiz.run()
    assert opened == ["https://console.groq.com/keys"]
    assert keys.get_key("groq") == "gsk_good_key_1234" and keys.get_key("gemini") is None
    assert [(p, m.id) for p, m in cfg.working_models()] == [("groq", "model-a")]
    assert all(password for prompt, password in script.prompts if "Paste" in prompt)  # keys typed hidden
    saved = UserConfig.load(tmp_path / "config.json")
    assert "groq" in saved.providers
    out = (tmp_path / "out.txt").read_text(encoding="utf-8")
    assert "gsk_good_key_1234" not in out  # the key is never printed


def test_wizard_warns_about_training(memory_keyring, tmp_path, monkeypatch):
    monkeypatch.setenv("SW_AGENT_HOME", str(tmp_path))
    out = tmp_path / "out.txt"
    wiz = Wizard(console=Console(file=open(out, "w", encoding="utf-8"), width=120), ask=Script(["q"]),
                 open_url=lambda u: None, discover_fn=fake_discover({}), providers=[_provider("gemini")],
                 config=UserConfig())
    wiz.run()
    text = out.read_text(encoding="utf-8")
    assert "MAY TRAIN" in text and "allowed to share" in text


# ---------------------------------------------------------------- other apps
def test_generated_app_files_are_in_sync():
    for rel, content in clients.render_project_files().items():
        assert (ROOT / rel).read_text(encoding="utf-8") == content, f"{rel} is stale: run `sw-agent sync`"


def test_static_app_configs_are_valid():
    vscode = json.loads((ROOT / ".vscode/mcp.json").read_text(encoding="utf-8"))
    assert vscode["servers"]["solidworks"]["args"] == ["-m", "sw_mcp"]
    gemini = json.loads((ROOT / ".gemini/settings.json").read_text(encoding="utf-8"))
    assert gemini["mcpServers"]["solidworks"]["args"] == ["-m", "sw_mcp"]


def test_claude_desktop_merge_keeps_other_servers(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    clients.add_to_claude_desktop(python="C:\\py\\python.exe", path=path)
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and "other" in data["mcpServers"]
    assert data["mcpServers"]["solidworks"]["command"] == "C:\\py\\python.exe"
    assert (tmp_path / "claude_desktop_config.json.bak").exists()
