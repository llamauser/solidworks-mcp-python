from __future__ import annotations

import json
import os
import zipfile

import pytest

from sw_agent import logs
from sw_mcp import config as sw_config


@pytest.fixture
def tmp_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(sw_config, "LOG_FILE", str(tmp_path / "sw_mcp" / "sw_mcp.log"))
    monkeypatch.setenv("SW_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def test_transcript_writes_one_line_per_event(tmp_logs):
    transcript = logs.Transcript("browser")
    transcript.write("user", "make a plate")
    transcript.write("tool", "build_part", data={"args": {"plan": "{}"}})
    records = [json.loads(line) for line in transcript.path.read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in records] == ["user", "tool"] and records[1]["data"]["args"]["plan"] == "{}"


def test_collect_zips_our_logs_and_opencodes_without_keys(tmp_logs):
    logs.log_dir().mkdir(parents=True, exist_ok=True)
    (logs.log_dir() / "sw_mcp.log").write_text("2026 INFO build_part ok\n", encoding="utf-8")
    (logs.log_dir() / "opencode-serve.log").write_text("opencode server listening\n", encoding="utf-8")
    oc_logs = tmp_logs / "home" / ".local" / "share" / "opencode" / "log"
    oc_logs.mkdir(parents=True)
    for i in range(5):
        f = oc_logs / f"2026-09-29T{i:02d}.log"
        f.write_text(f"opencode log {i}\n", encoding="utf-8")
        os.utime(f, (1_790_000_000 + i, 1_790_000_000 + i))
    logs.Transcript("browser").write("user", "make a plate")
    zip_path = logs.collect(dest_folder=tmp_logs)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "logs/sw_mcp.log" in names and "logs/opencode-serve.log" in names and "system.txt" in names
        assert sorted(n for n in names if n.startswith("opencode/")) == [
            "opencode/2026-09-29T02.log", "opencode/2026-09-29T03.log", "opencode/2026-09-29T04.log"]
        assert "config/opencode.json" in names and "config/solidworks.md" in names
        assert any(n.startswith("conversations/") for n in names)
        assert "opencode:" in zf.read("system.txt").decode()
        blob = b"".join(zf.read(n) for n in names)
    assert b"api_key" not in blob.lower() and b"bearer" not in blob.lower()
