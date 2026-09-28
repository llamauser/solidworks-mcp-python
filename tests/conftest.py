from __future__ import annotations

import json

import pytest

from sw_mcp.core import connection
from sw_mcp.core.resilience import runtime
from sw_mcp.fakes.fake_sw import FakeApp


@pytest.fixture(autouse=True)
def private_settings(monkeypatch, tmp_path_factory):
    """Settings a test saves (model choice, OpenAI extras) never touch the real %APPDATA%."""
    monkeypatch.setenv("SW_AGENT_HOME", str(tmp_path_factory.mktemp("sw_agent_home")))


@pytest.fixture(autouse=True)
def clean_runtime():
    """Every test starts with a closed breaker and no cached COM handle."""
    runtime.breaker.reset()
    runtime.worker.submit(connection.reset, 5)
    yield
    runtime.breaker.reset()
    runtime.worker.submit(connection.reset, 5)


@pytest.fixture
def fake_app(monkeypatch) -> FakeApp:
    app = FakeApp()
    monkeypatch.setattr(connection, "_attach_running", lambda: app)
    monkeypatch.setattr(connection, "solidworks_pids", lambda: [])
    return app


@pytest.fixture
def no_solidworks(monkeypatch):
    import pywintypes

    def fail():
        raise pywintypes.com_error(0x800401E3 - 2**32, "Operation unavailable", None, None)

    launched = []
    monkeypatch.setattr(connection, "_attach_running", fail)
    monkeypatch.setattr(connection, "solidworks_pids", lambda: [])
    monkeypatch.setattr(connection, "find_solidworks_exe", lambda: r"C:\SW\SLDWORKS.exe")
    monkeypatch.setattr(connection, "_launch", lambda exe: launched.append(exe))
    monkeypatch.setattr(connection, "_launched_at", None)
    return launched


def parse(raw: str) -> dict:
    assert isinstance(raw, str)
    return json.loads(raw)
