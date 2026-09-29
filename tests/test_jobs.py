"""Job records, ratings, sharing, and reusing well-rated builds as examples."""

from __future__ import annotations

import asyncio
import json
import zipfile

import pytest

from sw_agent import jobs
from sw_agent.assistant import Assistant, Decision, Toolbox
from sw_mcp.core import connection
from sw_mcp.fakes.fake_modeler import make_modeling_app
from sw_mcp.sw import modeling as m
from sw_mcp.sw import plan as p
from tests.test_agent_chat import Scripted, config_with, make_router, text, tool_call

PLATE = {"steps": [{"op": "box", "x": [-40, 40], "y": [0, 8], "z": [-25, 25]},
                   {"op": "cylinder", "mode": "cut", "start": [0, -1, 0], "end": [0, 9, 0], "diameter": 6}]}


@pytest.fixture
def fake_solidworks(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_last_group", {})
    monkeypatch.setattr(p, "_unsaved_failed_builds", [])
    monkeypatch.setenv("SW_MCP_PROJECTS", str(tmp_path / "projects"))
    app = make_modeling_app()
    connection.use_app_factory(lambda: app)
    yield app
    connection.use_app_factory(None)


def talk(llm, message, **kw):
    router, _ = make_router(config_with(("groq", "g", None, 100)), {"groq": llm})
    events = []

    async def go():
        async with Toolbox() as toolbox:
            assistant = Assistant(router, toolbox, on_event=events.append, **kw)
            reply = await assistant.send(message)
            return reply, assistant

    reply, assistant = asyncio.run(go())
    return reply, assistant, events


def test_a_build_is_recorded_with_steps_models_and_a_picture(fake_solidworks):
    llm = Scripted([tool_call("build_part", {"plan": json.dumps(PLATE), "save_as": "Plates/plate80"}), text("Done.")])
    reply, assistant, events = talk(llm, "plate 80 x 50 x 8 with a 6 mm hole", interface="browser")
    job_event = next(e for e in events if e.kind == "job")
    assert job_event.data["changed"] is True and job_event.data["id"] == assistant.last_job_id
    record = jobs.load(assistant.last_job_id)
    assert record["request"] == "plate 80 x 50 x 8 with a 6 mm hole" and record["reply"] == "Done."
    assert record["interface"] == "browser" and record["models"][0]["model"] == "Groq / g"
    step = record["steps"][0]
    assert step["tool"] == "build_part" and step["ok"] and step["model"] == "Groq / g"
    assert json.loads(step["args"]["plan"]) == PLATE  # the whole plan is kept
    assert record["project"] == "Plates" and record["result"]["parts"]["plate80"]["size_mm"] == [80.0, 8.0, 50.0]
    assert record["picture"].startswith("picture.") and record["versions"]["app"]
    assert (jobs.jobs_dir() / assistant.last_job_id / record["picture"]).exists()


def test_records_never_contain_paths_or_user_names(fake_solidworks, monkeypatch, tmp_path):
    monkeypatch.setenv("USERNAME", "meday")
    llm = Scripted([tool_call("open_document", {"file_path": r"C:\Users\meday\Documents\x.SLDPRT"}), text("No.")])
    _, assistant, _ = talk(llm, "open C:\\Users\\meday\\Documents\\x.SLDPRT and mail meday@example.com")
    raw = (jobs.jobs_dir() / assistant.last_job_id / "record.json").read_text(encoding="utf-8")
    assert "meday" not in raw and "example.com" not in raw and "USER" in raw


def test_a_question_is_recorded_but_not_offered_for_rating(fake_solidworks):
    _, assistant, events = talk(Scripted([text("Hello!")]), "hi")
    assert next(e for e in events if e.kind == "job").data["changed"] is False


def test_rating_and_sharing(fake_solidworks, tmp_path):
    llm = Scripted([tool_call("build_part", {"plan": json.dumps(PLATE)}), text("Done.")])
    _, assistant, _ = talk(llm, "plate")
    rating = jobs.rate(assistant.last_job_id, 4, ["wrong size", "not a tag"], "holes too small")
    assert rating["stars"] == 4 and rating["tags"] == ["wrong size"]
    assert jobs.load(assistant.last_job_id)["rating"]["comment"] == "holes too small"
    with pytest.raises(ValueError):
        jobs.rate("../../evil", 5)
    path, count = jobs.pack(tmp_path)
    assert count == 1
    names = zipfile.ZipFile(path).namelist()
    assert f"{assistant.last_job_id}/record.json" in names and "README.txt" in names
    assert any(n.startswith(f"{assistant.last_job_id}/picture.") for n in names)


def test_user_corrections_are_recorded(fake_solidworks):
    bigger = json.loads(json.dumps(PLATE))
    bigger["steps"][1]["diameter"] = 8
    llm = Scripted([tool_call("build_part", {"plan": json.dumps(PLATE)}), text("Done.")])

    async def approve(call):
        return Decision("run", {"plan": json.dumps(bigger)})

    _, assistant, _ = talk(llm, "plate", approver=approve, review="builds")
    action = jobs.load(assistant.last_job_id)["user_actions"][0]
    assert action["kind"] == "edited" and json.loads(action["user_version"]["plan"])["steps"][1]["diameter"] == 8
    assert json.loads(action["model_version"]["plan"])["steps"][1]["diameter"] == 6


def test_a_well_rated_build_becomes_an_example_for_a_similar_request(fake_solidworks):
    llm = Scripted([tool_call("build_part", {"plan": json.dumps(PLATE)}), text("Done.")])
    _, assistant, _ = talk(llm, "aluminium base plate 80 x 50 x 8 with a center hole")
    assert jobs.similar_example("base plate 90 x 60 x 10 with a center hole") == ""  # not rated yet
    jobs.rate(assistant.last_job_id, 5)
    example = jobs.similar_example("base plate 90 x 60 x 10 with a center hole")
    assert "rated 5/5" in example and "build_part" in example and '\\"box\\"' in example
    assert jobs.similar_example("V8 engine with a supercharger") == ""  # not similar
    llm2 = Scripted([text("ok")])
    _, _, events = talk(llm2, "base plate 90 x 60 x 10 with a center hole")
    assert "rated 5/5" in llm2.requests[0]["messages"][0]["content"]
    assert any(e.kind == "info" and "similar build" in e.text for e in events)


def test_scoring_models_leaves_no_records(fake_solidworks):
    _, assistant, _ = talk(Scripted([text("hi")]), "hi", record=False)
    assert assistant.last_job_id is None and not list(jobs.jobs_dir().glob("*"))
