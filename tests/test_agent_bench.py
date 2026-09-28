from __future__ import annotations

import asyncio
import json

import pytest

from sw_agent.bench import TASKS, estimate_requests, run_bench
from sw_agent.config import ModelEntry, UserConfig
from sw_agent.llm import ChatResult, LLMError
from sw_agent.registry import load_providers
from sw_agent.router import Router
from sw_mcp.sw import modeling as m

GOOD_PLANS = {
    "plate": {"steps": [
        {"op": "box", "x": [-30, 30], "y": [0, 10], "z": [-20, 20]},
        {"op": "cylinder", "mode": "cut", "start": [-22, -1, -12], "end": [-22, 11, -12], "diameter": 6},
        {"op": "repeat", "copies": 1, "step": [44, 0, 0]},
        {"op": "repeat", "copies": 1, "step": [0, 0, 24]}]},
    "flange": {"steps": [
        {"op": "cylinder", "start": [0, 0, 0], "end": [0, 10, 0], "diameter": 90},
        {"op": "cylinder", "mode": "cut", "start": [0, -1, 0], "end": [0, 11, 0], "diameter": 30},
        {"op": "cylinder", "mode": "cut", "start": [35, -1, 0], "end": [35, 11, 0], "diameter": 8},
        {"op": "repeat_around", "copies": 5, "angle_step": 60}]},
    "bracket": {"steps": [
        {"op": "box", "x": [-40, 40], "y": [0, 6], "z": [-20, 20]},
        {"op": "box", "x": [-40, 40], "y": [6, 50], "z": [-20, -14]},
        {"op": "cylinder", "mode": "cut", "start": [-25, -1, 5], "end": [-25, 7, 5], "diameter": 6},
        {"op": "repeat", "copies": 1, "step": [50, 0, 0]}]},
}


def task_of(messages) -> str:
    text = next(m["content"] for m in messages if m["role"] == "user")
    return "flange" if "flange" in text else "bracket" if "L-bracket" in text else "plate"


class Model:
    """Scripted model: sends a plan for the task, then a final sentence."""

    def __init__(self, sloppy: bool = False, rate_limited: bool = False):
        self.sloppy = sloppy
        self.rate_limited = rate_limited

    def chat(self, model, messages, tools=None, tool_choice=None, max_tokens=None, temperature=0.1):
        if self.rate_limited:
            raise LLMError("rate_limit", "429", 429, retry_after=60)
        if messages[-1]["role"] == "tool":
            return ChatResult("Done.", [], {}, {}, 5)
        plan = json.loads(json.dumps(GOOD_PLANS[task_of(messages)]))
        if self.sloppy:
            first = plan["steps"][0]
            if first["op"] == "box":
                first["z"] = [-10, 10]  # base too narrow
            else:
                first["diameter"] = 80
        call = {"id": "c1", "type": "function", "function": {"name": "build_part",
                                                             "arguments": json.dumps({"plan": json.dumps(plan)})}}
        return ChatResult("", [call], {}, {}, 5)


@pytest.fixture(autouse=True)
def fresh_groups(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_last_group", {})
    monkeypatch.setenv("SW_AGENT_HOME", str(tmp_path))


def test_good_plans_pass_every_check():
    # guards the benchmark itself: correct plans must score 100
    cfg = UserConfig()
    cfg.record("groq", [ModelEntry("good", True, 100)])
    router = Router(cfg, providers=load_providers(), key_for=lambda p: "k",
                    client_factory=lambda p, k: Model())
    scores = asyncio.run(run_bench(cfg, router, on_progress=lambda s: None))
    assert scores[0].score == 100.0, [r.detail for r in scores[0].results]


def test_scores_rank_models_and_are_saved():
    cfg = UserConfig()
    cfg.record("groq", [ModelEntry("sloppy", True, 100)])
    cfg.record("gemini", [ModelEntry("careful", True, 3000), ModelEntry("busy", True, 50)])
    models = {"sloppy": Model(sloppy=True), "careful": Model(), "busy": Model(rate_limited=True)}

    class ByModel:
        def __init__(self, provider):
            self.provider = provider

        def chat(self, model, *a, **kw):
            return models[model].chat(model, *a, **kw)

    router = Router(cfg, providers=load_providers(), key_for=lambda p: "k",
                    client_factory=lambda p, k: ByModel(p))
    lines = []
    scores = {s.candidate.model: s for s in asyncio.run(run_bench(cfg, router, max_models=3, on_progress=lines.append))}
    assert scores["careful"].score == 100.0
    assert scores["sloppy"].score == 0.0 and "want" in scores["sloppy"].results[0].detail
    assert scores["busy"].score is None and "rate-limited" in scores["busy"].skipped
    saved = UserConfig.load()
    assert [m.id for _, m in saved.working_models()][0] == "careful"  # the router now prefers it
    assert any("PASS" in line for line in lines)


def test_request_estimate():
    assert estimate_requests(2) == 2 * len(TASKS) * 3
