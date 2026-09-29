"""OpenCode runs the conversation; this module starts it and talks to it.

- find_opencode(): the opencode executable (PATH, then the official install folder, then npm's).
- OpenCodeServer: `opencode serve` in the background, on 127.0.0.1 and a free port, protected by
  a random password (OPENCODE_SERVER_PASSWORD), started in the project folder so it loads
  opencode.json (the SolidWorks MCP server) and the "solidworks" agent.
- OpenCodeClient: the few HTTP calls the browser app needs (sessions, prompts, abort, models,
  permission answers) and the event stream (Server-Sent Events on /event).
- run_tui(): the terminal mode, OpenCode's own interface with the solidworks agent.

Checked against opencode 1.18 (`opencode serve`, OpenAPI at /doc).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("sw_agent.opencode")

AGENT = "solidworks"
ROOT = Path(__file__).resolve().parents[2]  # the project folder with opencode.json


class OpenCodeMissing(RuntimeError):
    pass


def find_opencode() -> str | None:
    found = shutil.which("opencode")
    if found:
        return found
    home = Path(os.path.expanduser("~"))
    for candidate in (home / ".opencode" / "bin" / "opencode.exe",
                      Path(os.environ.get("APPDATA", "")) / "npm" / "opencode.cmd"):
        if candidate.is_file():
            return str(candidate)
    return None


def require_opencode() -> str:
    exe = find_opencode()
    if not exe:
        raise OpenCodeMissing("OpenCode is not installed. Run 'Install SolidWorks Assistant.bat' "
                              "(or Tools menu, option 8) to install it.")
    return exe


def version(exe: str | None = None) -> str:
    try:
        out = subprocess.run([exe or require_opencode(), "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError, OpenCodeMissing):
        return ""


def run_tui(extra: list[str] | None = None) -> int:
    """OpenCode's own terminal interface, in the project folder, with the solidworks agent."""
    return subprocess.call([require_opencode(), "--agent", AGENT, *(extra or [])], cwd=ROOT)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------- the background server
class OpenCodeServer:
    """`opencode serve` for the lifetime of the browser app."""

    def __init__(self, exe: str | None = None, cwd: Path = ROOT, log_file: Path | None = None) -> None:
        self.exe = exe or require_opencode()
        self.cwd = cwd
        self.port = free_port()
        self.password = secrets.token_urlsafe(24)
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_file = log_file
        self.process: subprocess.Popen | None = None

    def start(self, wait_s: float = 90.0) -> "OpenCodeServer":
        env = {**os.environ, "OPENCODE_SERVER_PASSWORD": self.password}
        out = open(self.log_file, "a", encoding="utf-8") if self.log_file else subprocess.DEVNULL  # noqa: SIM115
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen([self.exe, "serve", "--hostname", "127.0.0.1", "--port", str(self.port)],
                                        cwd=self.cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                        creationflags=flags)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"OpenCode stopped while starting (exit code {self.process.returncode}); "
                                   f"see {self.log_file}.")
            try:
                r = httpx.get(self.url + "/global/health", auth=("opencode", self.password), timeout=2)
                if r.status_code == 200 and r.json().get("healthy"):
                    log.info("opencode %s is serving on %s", r.json().get("version"), self.url)
                    return self
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError(f"OpenCode did not start within {wait_s:.0f} s; see {self.log_file}.")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def client(self) -> "OpenCodeClient":
        return OpenCodeClient(self.url, self.password)


# ---------------------------------------------------------------- the HTTP client
class OpenCodeClient:
    def __init__(self, url: str, password: str, http: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.http = http or httpx.AsyncClient(base_url=self.url, auth=("opencode", password),
                                              timeout=httpx.Timeout(60, read=None))

    async def close(self) -> None:
        await self.http.aclose()

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.http.request(method, path, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    async def health(self) -> dict:
        return await self._json("GET", "/global/health")

    async def create_session(self, title: str = "SolidWorks Assistant") -> dict:
        return await self._json("POST", "/session", json={"title": title})

    async def prompt(self, session_id: str, text: str, model: dict | None = None, system: str = "") -> None:
        """Send a message and return at once; the answer arrives on the event stream."""
        body: dict[str, Any] = {"agent": AGENT, "parts": [{"type": "text", "text": text}]}
        if model:
            body["model"] = model
        if system:
            body["system"] = system
        r = await self.http.post(f"/session/{session_id}/prompt_async", json=body)
        r.raise_for_status()

    async def abort(self, session_id: str) -> None:
        await self._json("POST", f"/session/{session_id}/abort")

    async def messages(self, session_id: str) -> list[dict]:
        return await self._json("GET", f"/session/{session_id}/message")

    async def models(self) -> list[dict]:
        """Connected providers' models: [{"providerID", "modelID", "name", "provider"}], default first."""
        data = await self._json("GET", "/config/providers")
        defaults = data.get("default") or {}
        out = []
        for provider in data.get("providers") or []:
            for mid, model in (provider.get("models") or {}).items():
                out.append({"providerID": provider["id"], "modelID": mid,
                            "name": model.get("name") or mid, "provider": provider.get("name") or provider["id"],
                            "default": defaults.get(provider["id"]) == mid})
        return out

    async def reply_permission(self, request_id: str, reply: str, message: str = "") -> None:
        body: dict[str, Any] = {"reply": reply}
        if message:
            body["message"] = message
        await self._json("POST", f"/permission/{request_id}/reply", json=body)

    async def events(self) -> AsyncIterator[dict]:
        """OpenCode's event stream (reconnects if it drops)."""
        while True:
            try:
                async with self.http.stream("GET", "/event") as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            try:
                                yield json.loads(line[5:].strip())
                            except ValueError:
                                continue
            except httpx.HTTPError as exc:
                log.warning("event stream dropped (%s); reconnecting", exc)
                await asyncio.sleep(1)
