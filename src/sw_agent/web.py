"""The browser version of the SolidWorks Assistant (`sw-agent web`).

A small local web server (127.0.0.1 only) that serves one page and a JSON API. The page polls
for events (thinking / tool / result / model / reply) so progress shows up while SolidWorks
works. Every API call must carry the random token embedded in the page, so other websites
open in the same browser cannot drive SolidWorks.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .assistant import LEAN_TOOLS, Assistant, Toolbox
from .config import UserConfig
from .logs import Transcript, setup_logging
from .router import Router

POLL_WAIT_S = 20.0
MAX_EVENTS = 2000


@dataclass
class WebSession:
    """Everything one browser session needs: the assistant and its event log."""

    assistant: Assistant
    router: Router
    events: list[dict] = field(default_factory=list)
    busy: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    next_id: int = 1

    def push(self, kind: str, text: str) -> None:
        self.events.append({"id": self.next_id, "kind": kind, "text": text})
        self.next_id += 1
        del self.events[:-MAX_EVENTS]
        self.changed.set()


SessionFactory = Callable[[], Any]  # returns an async context manager yielding (router, toolbox)


@asynccontextmanager
async def default_backend():
    config = UserConfig.load()
    router = Router(config)
    async with Toolbox(LEAN_TOOLS) as toolbox:
        yield router, toolbox


def create_app(backend: SessionFactory = default_backend, token: str | None = None,
               transcript_factory: Callable[[], Any] = lambda: None) -> Starlette:
    token = token or secrets.token_urlsafe(24)
    state: dict[str, WebSession] = {}

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with backend() as (router, toolbox):
            session = WebSession(assistant=None, router=router)  # type: ignore[arg-type]
            session.assistant = Assistant(router, toolbox, on_event=lambda e: session.push(e.kind, e.text),
                                          transcript=transcript_factory())
            state["s"] = session
            if not router.candidates():
                session.push("error", "No AI model is connected yet. Close this window and run "
                                      "'sw-agent setup' (or re-run the installer), then start again.")
            yield

    def authorized(request: Request) -> bool:
        return secrets.compare_digest(request.headers.get("x-token", ""), token)

    def denied() -> JSONResponse:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    async def page(request: Request) -> HTMLResponse:
        html = resources.files("sw_agent").joinpath("web.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__TOKEN__", token), headers={"Cache-Control": "no-store"})

    async def send(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        s = state["s"]
        text = str((await request.json()).get("text", "")).strip()
        if not text:
            return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
        if s.busy:
            return JSONResponse({"ok": False, "error": "busy"}, status_code=409)
        s.busy = True
        s.push("user", text)

        async def run() -> None:
            try:
                await s.assistant.send(text)
            except Exception as exc:  # noqa: BLE001 - show it instead of dying silently
                s.push("error", f"Something went wrong: {exc}")
            finally:
                s.busy = False
                s.push("idle", "")

        asyncio.get_running_loop().create_task(run())
        return JSONResponse({"ok": True})

    async def events(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        s = state["s"]
        after = int(request.query_params.get("after", "0") or 0)
        wait = request.query_params.get("wait", "1") != "0"
        if wait and not any(e["id"] > after for e in s.events):
            s.changed.clear()
            try:
                await asyncio.wait_for(s.changed.wait(), POLL_WAIT_S)
            except asyncio.TimeoutError:
                pass
        return JSONResponse({"events": [e for e in s.events if e["id"] > after], "busy": s.busy})

    async def status(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        s = state["s"]
        models = [{"n": i, "provider": p, "model": m, "state": st} for i, (p, m, st) in enumerate(s.router.status(), 1)]
        return JSONResponse({"models": models, "busy": s.busy, "pinned": bool(s.router.pinned)})

    async def control(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        s = state["s"]
        body = await request.json()
        action = body.get("action")
        if action == "new":
            if s.busy:
                return JSONResponse({"ok": False, "error": "busy"}, status_code=409)
            s.assistant.reset()
            s.push("info", "New conversation.")
        elif action == "use":
            cands = s.router.candidates()
            n = int(body.get("n", 0))
            if not 1 <= n <= len(cands):
                return JSONResponse({"ok": False, "error": "no such model"}, status_code=400)
            s.router.pin(cands[n - 1].provider.id, cands[n - 1].model)
            s.push("info", f"Using {cands[n - 1].label} first.")
        elif action == "auto":
            s.router.unpin()
            s.push("info", "Automatic model choice.")
        else:
            return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/", page),
            Route("/api/send", send, methods=["POST"]),
            Route("/api/events", events),
            Route("/api/status", status),
            Route("/api/control", control, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def free_port(preferred: int = 8777) -> int:
    for port in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred


def main(open_browser: bool = True, port: int | None = None) -> int:
    import uvicorn

    setup_logging()
    port = port or free_port()
    url = f"http://127.0.0.1:{port}/"
    print(f"SolidWorks Assistant is running at {url}  (close this window to stop it)")
    if open_browser:
        import threading

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(transcript_factory=lambda: Transcript("browser")), host="127.0.0.1", port=port,
                log_level="warning")
    return 0
