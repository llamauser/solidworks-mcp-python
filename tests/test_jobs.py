"""Job records, ratings, sharing, and reusing well-rated builds as examples."""

from __future__ import annotations

import json
import zipfile

import pytest

from sw_agent import jobs

PLATE = {"steps": [{"op": "box", "x": [-40, 40], "y": [0, 8], "z": [-25, 25]}]}


def a_job(request="plate 80 x 50 x 8 with a hole", changed=True, stars=None) -> str:
    job = jobs.Job(request, "browser")
    job.model("opencode/nemotron-3-ultra-free", {"prompt_tokens": 1200, "completion_tokens": 40})
    output = json.dumps({"ok": True, "part": "Part1", "size_mm": [80.0, 8.0, 50.0]}) if changed else \
        json.dumps({"ok": True, "solidworks": "ready"})
    job.step("build_part" if changed else "get_status", {"plan": json.dumps(PLATE)} if changed else {}, output,
             "opencode/nemotron-3-ultra-free", 2.5)
    job.finish("Done.", None, None, None)
    if stars:
        jobs.rate(job.id, stars)
    return job.id


def test_a_record_keeps_the_request_steps_and_models():
    job_id = a_job()
    record = jobs.load(job_id)
    assert record["request"] == "plate 80 x 50 x 8 with a hole" and record["reply"] == "Done."
    assert record["changed_solidworks"] is True and record["models"][0]["tokens_in"] == 1200
    step = record["steps"][0]
    assert step["tool"] == "build_part" and step["ok"] and json.loads(step["args"]["plan"]) == PLATE
    assert record["versions"]["app"] and record["versions"]["guide"]


def test_records_never_contain_paths_or_user_names(monkeypatch):
    monkeypatch.setenv("USERNAME", "meday")
    job = jobs.Job(r"open C:\Users\meday\Documents\x.SLDPRT and mail meday@example.com", "browser")
    job.step("open_document", {"file_path": r"C:\Users\meday\Documents\x.SLDPRT"}, json.dumps({"ok": True}), "m", 1)
    job.finish("No.", None, None, None)
    raw = (jobs.jobs_dir() / job.id / "record.json").read_text(encoding="utf-8")
    assert "meday" not in raw and "example.com" not in raw and "USER" in raw


def test_rating_and_sharing(tmp_path):
    job_id = a_job()
    rating = jobs.rate(job_id, 4, ["wrong size", "not a tag"], "holes too small")
    assert rating["stars"] == 4 and rating["tags"] == ["wrong size"]
    assert jobs.load(job_id)["rating"]["comment"] == "holes too small"
    with pytest.raises(ValueError):
        jobs.rate("../../evil", 5)
    (jobs.jobs_dir() / job_id / "picture.png").write_bytes(b"png")
    path, count = jobs.pack(tmp_path)
    assert count == 1
    names = zipfile.ZipFile(path).namelist()
    assert f"{job_id}/record.json" in names and f"{job_id}/picture.png" in names and "README.txt" in names


def test_a_well_rated_build_becomes_an_example_for_a_similar_request():
    job_id = a_job("aluminium base plate 80 x 50 x 8 with a center hole")
    assert jobs.similar_example("base plate 90 x 60 x 10 with a center hole") == ""  # not rated yet
    jobs.rate(job_id, 5)
    example = jobs.similar_example("base plate 90 x 60 x 10 with a center hole")
    assert "rated 5/5" in example and "build_part" in example and '\\"box\\"' in example
    assert jobs.similar_example("V8 engine with a supercharger") == ""  # not similar
    a_job("a question about bolts", changed=False, stars=5)
    assert "get_status" not in jobs.similar_example("a question about bolts")  # no build to copy
