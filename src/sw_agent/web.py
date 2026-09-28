"""The browser version of the SolidWorks Assistant (`sw-agent web`).

A small local web server (127.0.0.1 only) that serves one page and a JSON API. The page polls
for events (thinking / tool / result / model / reply / review) so progress shows up while
SolidWorks works. In review mode the assistant waits before changing SolidWorks: the page shows
the call (a plan as readable steps), and the user runs it, edits it, skips it with a note, or
stops. Every API call must carry the random token embedded in the page, so other websites open
in the same browser cannot drive SolidWorks.
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

from .assistant import LEAN_TOOLS, REVIEW_TOOLS, Assistant, Decision, PendingCall, Toolbox
from .config import UserConfig
from .logs import Transcript, setup_logging
from .review import edited_args, preview_call
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
    pending: dict | None = None  # the call waiting for the user: {"payload", "future"}
    next_review: int = 1

    def push(self, kind: str, text: str, data: dict | None = None) -> None:
        event = {"id": self.next_id, "kind": kind, "text": text}
        if data is not None:
            event["data"] = data
        self.events.append(event)
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

            async def approve(call: PendingCall) -> Decision:
                future: asyncio.Future = asyncio.get_running_loop().create_future()
                payload = {"id": session.next_review, "name": call.name, "args": call.args, "model": call.model,
                           "reason": call.reason, "preview": preview_call(call.name, call.args)}
                session.next_review += 1
                session.pending = {"payload": payload, "future": future}
                session.push("review", f"Waiting for you: {call.name}", payload)
                try:
                    return await future
                finally:
                    session.pending = None

            session.assistant = Assistant(router, toolbox, on_event=lambda e: session.push(e.kind, e.text, e.data),
                                          transcript=transcript_factory(), approver=approve)
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
        return JSONResponse({"models": models, "busy": s.busy, "pinned": bool(s.router.pinned),
                             "review": s.assistant.review, "pending": s.pending["payload"] if s.pending else None})

    async def providers(request: Request) -> JSONResponse:
        """Providers the user can pick a model from (a key is stored, or it runs on this PC)."""
        if not authorized(request):
            return denied()
        s = state["s"]
        rows = [{"id": p.id, "name": p.name, "paid": p.paid} for p in s.router.providers.values()
                if s.router.has_key(p.id)]
        manual = dict(s.router.config.manual or {})
        return JSONResponse({"providers": rows, "manual": manual,
                             "openai_tools": s.router.config.openai_tools or {}})

    async def models(request: Request) -> JSONResponse:
        """The models a provider offers right now (asked live), best-suited first."""
        if not authorized(request):
            return denied()
        s = state["s"]
        pid = request.query_params.get("provider", "")
        provider = s.router.providers.get(pid)
        if provider is None or not s.router.has_key(pid):
            return JSONResponse({"ok": False, "error": "unknown provider or no key"}, status_code=400)

        def ask() -> list[str]:
            from .probe import rank_models

            return rank_models(provider, s.router.client_for(provider).list_models())

        try:
            listed = await asyncio.to_thread(ask)
        except Exception as exc:  # noqa: BLE001 - show the reason instead of failing the page
            return JSONResponse({"ok": False, "error": f"Could not list the models: {exc}"})
        tested = {m.id for m in (s.router.config.providers.get(pid).models if pid in s.router.config.providers else [])
                  if m.tools_ok}
        return JSONResponse({"ok": True, "models": listed[:300], "tested": sorted(tested)})

    async def context(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        return JSONResponse(state["s"].assistant.context())

    async def preview(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        body = await request.json()
        args = edited_args({}, body.get("args"))
        return JSONResponse(preview_call(str(body.get("name", "")), args) or {"ok": True})

    async def review(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        s = state["s"]
        body = await request.json()
        pending = s.pending
        if pending is None or body.get("id") != pending["payload"]["id"] or pending["future"].done():
            return JSONResponse({"ok": False, "error": "nothing is waiting for you"}, status_code=409)
        action = body.get("action")
        if action not in ("run", "skip", "stop"):
            return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)
        args = edited_args(pending["payload"]["args"], body.get("args")) if action == "run" else None
        pending["future"].set_result(Decision(action, args, str(body.get("note", ""))[:1000]))
        return JSONResponse({"ok": True})

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
            s.router.unpin(remember=True)
            s.push("info", "Automatic model choice.")
        elif action == "pick":
            pid, model = str(body.get("provider", "")), str(body.get("model", "")).strip()
            if not model or pid not in s.router.providers or not s.router.has_key(pid):
                return JSONResponse({"ok": False, "error": "choose a connected provider and a model"}, status_code=400)
            only = bool(body.get("only"))
            s.router.pin(pid, model, only=only, remember=True)
            s.push("info", f"Using {s.router.providers[pid].name} / {model}"
                           + (" only (no switching)." if only else " first."))
        elif action == "openai_tools":
            tools = {"internet": bool(body.get("internet")), "terminal": bool(body.get("terminal"))}
            s.router.config.openai_tools = tools
            try:
                s.router.config.save()
            except OSError:
                pass
            s.push("info", "OpenAI models: internet " + ("ON" if tools["internet"] else "off") + ", terminal commands "
                   + ("ON (each command waits for your OK)" if tools["terminal"] else "off") + ".")
        elif action == "stop":
            if not s.busy:
                return JSONResponse({"ok": False, "error": "nothing is running"}, status_code=409)
            s.assistant.request_stop()
            if s.pending and not s.pending["future"].done():
                s.pending["future"].set_result(Decision("stop"))
            s.push("info", "Stopping after the current step...")
        elif action == "review":
            mode = body.get("mode")
            if mode not in REVIEW_TOOLS:
                return JSONResponse({"ok": False, "error": "unknown mode"}, status_code=400)
            s.assistant.review = mode
            s.push("info", {"off": "Review is off: I build without asking.",
                            "builds": "Review is on: I show you each plan before building it.",
                            "all": "Review everything: I ask before every change in SolidWorks."}[mode])
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
            Route("/api/context", context),
            Route("/api/providers", providers),
            Route("/api/models", models),
            Route("/api/preview", preview, methods=["POST"]),
            Route("/api/review", review, methods=["POST"]),
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
