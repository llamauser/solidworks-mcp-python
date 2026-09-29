"""The OpenCode client (exact paths and bodies of opencode 1.18's server), the agent file, and the CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from sw_agent import agentfile, opencode
from sw_agent import __main__ as cli


def client_with(handler) -> opencode.OpenCodeClient:
    http = httpx.AsyncClient(base_url="http://oc", transport=httpx.MockTransport(handler), auth=("opencode", "pw"))
    return opencode.OpenCodeClient("http://oc", "pw", http=http)


def test_client_sends_the_right_requests():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body, request.headers.get("authorization", "")))
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/config/providers":
            return httpx.Response(200, json={"default": {"opencode": "big-pickle"}, "providers": [
                {"id": "opencode", "name": "OpenCode Zen", "models": {"big-pickle": {"name": "Big Pickle"},
                                                                      "nemotron-3-ultra-free": {}}}]})
        if request.url.path.endswith("/prompt_async"):
            return httpx.Response(204)
        return httpx.Response(200, json=True)

    async def go():
        c = client_with(handler)
        sid = (await c.create_session())["id"]
        await c.prompt(sid, "make a plate", {"providerID": "opencode", "modelID": "big-pickle"}, "CURRENT DESIGN ...")
        await c.prompt(sid, "again")
        await c.abort(sid)
        await c.reply_permission("per_1", "always")
        models = await c.models()
        await c.close()
        return models

    models = asyncio.run(go())
    assert seen[0][:3] == ("POST", "/session", {"title": "SolidWorks Assistant"})
    assert seen[0][3].startswith("Basic ")
    assert seen[1][:3] == ("POST", "/session/ses_1/prompt_async", {
        "agent": "solidworks", "parts": [{"type": "text", "text": "make a plate"}],
        "model": {"providerID": "opencode", "modelID": "big-pickle"}, "system": "CURRENT DESIGN ..."})
    assert seen[2][2] == {"agent": "solidworks", "parts": [{"type": "text", "text": "again"}]}
    assert seen[3][:2] == ("POST", "/session/ses_1/abort")
    assert seen[4][:3] == ("POST", "/permission/per_1/reply", {"reply": "always"})
    assert models[0] == {"providerID": "opencode", "modelID": "big-pickle", "name": "Big Pickle",
                         "provider": "OpenCode Zen", "default": True}
    assert models[1]["name"] == "nemotron-3-ultra-free" and not models[1]["default"]


def test_event_stream_is_parsed():
    stream = ("data: {\"type\":\"server.connected\",\"properties\":{}}\n\n"
              ": comment\n\ndata: not json\n\n"
              "data: {\"type\":\"session.idle\",\"properties\":{\"sessionID\":\"ses_1\"}}\n\n")

    def handler(request):
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    async def go():
        c = client_with(handler)
        out = []
        async for ev in c.events():
            out.append(ev["type"])
            if len(out) == 2:
                break
        return out

    assert asyncio.run(go()) == ["server.connected", "session.idle"]


def test_agent_file_is_generated_from_the_guide(tmp_path):
    assert agentfile.sync(tmp_path) is True and agentfile.sync(tmp_path) is False
    text = (tmp_path / agentfile.AGENT_FILE).read_text(encoding="utf-8")
    assert text.startswith("---\n") and "solidworks_*: allow" in text and "bash: ask" in text
    assert agentfile.guide_text() in text
    repo = Path(__file__).resolve().parents[1]
    assert (repo / agentfile.AGENT_FILE).read_text(encoding="utf-8") == agentfile.render(), \
        "the committed agent file is stale: run `sw-agent sync`"


def test_find_opencode_looks_in_the_install_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode.shutil, "which", lambda name: None)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert opencode.find_opencode() is None
    with pytest.raises(opencode.OpenCodeMissing):
        opencode.require_opencode()
    exe = tmp_path / ".opencode" / "bin" / "opencode.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert opencode.find_opencode() == str(exe)


def test_terminal_mode_opens_opencode_with_the_solidworks_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode, "find_opencode", lambda: "opencode.exe")
    monkeypatch.setattr(opencode.subprocess, "call", lambda args, cwd=None: calls.append((args, cwd)) or 0)
    monkeypatch.setattr(agentfile, "sync", lambda root: False)
    assert cli.main(["cli"]) == 0
    assert calls == [(["opencode.exe", "--agent", "solidworks"], opencode.ROOT)]


def test_commands_explain_when_opencode_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(opencode, "find_opencode", lambda: None)
    assert cli.main(["cli"]) == 1 and cli.main(["web", "--no-browser"]) == 1
    assert "OpenCode is not installed" in capsys.readouterr().out
