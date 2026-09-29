from __future__ import annotations

import asyncio
import json
import zipfile

import pytest

from sw_agent import logs
from sw_agent.assistant import Assistant, Toolbox
from sw_agent.config import ModelEntry, UserConfig
from sw_agent.llm import ChatResult
from sw_agent.registry import load_providers
from sw_agent.router import Router
from sw_mcp import config as sw_config
from sw_mcp.core import connection
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import modeling as m

PLAN = {"steps": [{"op": "box", "x": [-20, 20], "y": [0, 5], "z": [-10, 10]}]}


class Scripted:
    def chat(self, model, messages, tools=None, tool_choice=None, max_tokens=None, temperature=0.1):
        if messages[-1]["role"] == "tool":
            return ChatResult("Done.", [], {}, {"prompt_tokens": 900, "completion_tokens": 12}, 5)
        call = {"id": "c1", "type": "function",
                "function": {"name": "build_part", "arguments": json.dumps({"plan": json.dumps(PLAN)})}}
        return ChatResult("", [call], {}, {"prompt_tokens": 800, "completion_tokens": 60}, 5)


@pytest.fixture
def tmp_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(sw_config, "LOG_FILE", str(tmp_path / "sw_mcp" / "sw_mcp.log"))
    monkeypatch.setenv("SW_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setattr(m, "_last_group", {})
    return tmp_path


def test_transcript_records_the_whole_conversation(tmp_logs):
    fake = make_modeling_app()
    connection.use_app_factory(lambda: fake)
    cfg = UserConfig()
    cfg.record("groq", [ModelEntry("m", True, 5)])
    router = Router(cfg, providers=load_providers(), key_for=lambda p: "k", client_factory=lambda p, k: Scripted())
    transcript = logs.Transcript("test")

    async def go():
        async with Toolbox() as toolbox:
            return await Assistant(router, toolbox, transcript=transcript).send("a small plate")

    try:
        assert asyncio.run(go()) == "Done."
    finally:
        connection.use_app_factory(None)
    records = [json.loads(line) for line in transcript.path.read_text(encoding="utf-8").splitlines()]
    kinds = [r["kind"] for r in records]
    assert kinds[0] == "user" and "tool_call" in kinds and "tool_output" in kinds and kinds[-2:] == ["reply", "job"]
    call = next(r for r in records if r["kind"] == "tool_call")
    assert call["text"] == "build_part" and '\\"box\\"' in call["arguments"]  # the full plan is kept
    answer = next(r for r in records if r["kind"] == "model_answer")
    assert answer["model"].endswith("/ m") and answer["usage"]["prompt_tokens"] == 800


def test_collect_zips_logs_without_keys(tmp_logs):
    logs.log_dir().mkdir(parents=True, exist_ok=True)
    (logs.log_dir() / "sw_mcp.log").write_text("2026 INFO build_part ok\n", encoding="utf-8")
    logs.Transcript("browser").write("user", "make a plate")
    cfg = UserConfig()
    cfg.record("groq", [ModelEntry("m", True, 5)])
    cfg.save()
    zip_path = logs.collect(dest_folder=tmp_logs)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "logs/sw_mcp.log" in names and "provider_tests.json" in names and "system.txt" in names
        assert any(n.startswith("conversations/") for n in names)
        blob = b"".join(zf.read(n) for n in names)
    assert b"api_key" not in blob.lower() and b"bearer" not in blob.lower()
