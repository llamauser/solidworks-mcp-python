"""The browser version of the SolidWorks Assistant (`sw-agent web`), a front end for OpenCode.

On start it runs `opencode serve` in the background (project folder, so opencode.json and the
"solidworks" agent apply) and opens a local page. Messages typed there go to OpenCode; OpenCode's
event stream comes back as progress lines (each SolidWorks step, with its arguments and result),
questions for permission, and the answer.

Around OpenCode it adds what this project needs: the shared design of the active project and a
well-rated similar past build are given to the model as extra instructions, every request is
recorded for the rating/sharing flow (jobs.py), and the page can stop a run or pick a model.

Security: the page and its API are on 127.0.0.1 only and every API call carries a random token
embedded in the page; OpenCode itself is protected by its own random password.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from . import jobs
from .logs import Transcript, log_dir, setup_logging
from .opencode import OpenCodeClient, OpenCodeServer, free_port
from .preview import preview_call, summarize_result

log = logging.getLogger("sw_agent.web")
POLL_WAIT_S = 20.0
MAX_EVENTS = 2000
PROJECT_ARGS = ("plan_machine", "make_assembly", "list_project", "make_engine")


@dataclass
class WebSession:
    client: Any
    session_id: str = ""
    events: list[dict] = field(default_factory=list)
    busy: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    next_id: int = 1
    model: dict | None = None            # chosen by the user; None = the agent's own model
    last_model: str = ""
    permissions: dict[str, dict] = field(default_factory=dict)  # open permission questions
    roles: dict[str, str] = field(default_factory=dict)         # messageID -> user/assistant
    part_kind: dict[str, str] = field(default_factory=dict)     # partID -> text/reasoning/tool
    texts: dict[str, dict[str, str]] = field(default_factory=dict)  # assistant messageID -> partID -> text
    turn_messages: list[str] = field(default_factory=list)      # assistant messages of this request
    tools: dict[str, str] = field(default_factory=dict)         # callID -> announced/done
    job: jobs.Job | None = None
    active_project: str | None = None
    transcript: Any = None
    models_cache: tuple[float, list] = (0.0, [])

    def push(self, kind: str, text: str, data: dict | None = None) -> None:
        event = {"id": self.next_id, "kind": kind, "text": text}
        if data is not None:
            event["data"] = data
        self.events.append(event)
        self.next_id += 1
        del self.events[:-MAX_EVENTS]
        self.changed.set()
        if self.transcript is not None and kind not in ("thinking",):
            self.transcript.write(kind, text, **({"data": data} if data and kind != "result" else {}))


# ---------------------------------------------------------------- OpenCode events -> page events
def note_project(s: WebSession, tool: str, args: dict) -> None:
    save_as = str(args.get("save_as") or "").replace("\\", "/")
    if "/" in save_as:
        s.active_project = save_as.split("/")[0].strip() or s.active_project
    elif tool in PROJECT_ARGS and str(args.get("project") or "").strip():
        s.active_project = str(args["project"]).strip()


def extra_instructions(s: WebSession, text: str) -> str:
    """Shared design of the active project + a well-rated similar build, for the model."""
    parts = []
    if s.active_project:
        try:
            from sw_mcp.sw import design

            parts.append(design.summary(s.active_project))
        except Exception:  # noqa: BLE001
            log.warning("could not read the design of %s", s.active_project, exc_info=True)
    try:
        example = jobs.similar_example(text)
    except Exception:  # noqa: BLE001
        example = ""
    if example:
        parts.append(example)
        s.push("info", "Using a similar build you rated well as an example.")
    return "\n\n".join(p for p in parts if p)


def handle_event(s: WebSession, ev: dict) -> bool:
    """Turn one OpenCode event into page events. Returns True when the request has finished."""
    kind = ev.get("type", "")
    props = ev.get("properties") or {}
    if props.get("sessionID") not in (None, s.session_id):
        return False
    if kind == "message.updated":
        info = props.get("info") or {}
        s.roles[info.get("id", "")] = info.get("role", "")
        if info.get("role") == "assistant":
            if info["id"] not in s.turn_messages:
                s.turn_messages.append(info["id"])
            label = f"{info.get('providerID', '')}/{info.get('modelID', '')}".strip("/")
            if label and label != s.last_model:
                s.last_model = label
                s.push("model", f"Model: {label}")
            if s.job is not None and info.get("time", {}).get("completed"):
                s.job.model(label, {"prompt_tokens": (info.get("tokens") or {}).get("input"),
                                    "completion_tokens": (info.get("tokens") or {}).get("output")})
            if info.get("error"):
                data = info["error"].get("data") or {}
                s.push("error", str(data.get("message") or info["error"].get("name") or "The model reported an error."))
    elif kind == "message.part.updated":
        part = props.get("part") or {}
        pid, ptype = part.get("id", ""), part.get("type", "")
        s.part_kind[pid] = ptype
        if ptype == "text" and s.roles.get(part.get("messageID")) != "user":
            s.texts.setdefault(part.get("messageID", ""), {})[pid] = part.get("text", "")
        elif ptype == "tool":
            handle_tool(s, part)
    elif kind == "message.part.delta":
        pid = props.get("partID", "")
        if s.part_kind.get(pid) == "text" and props.get("field") == "text":
            texts = s.texts.setdefault(props.get("messageID", ""), {})
            texts[pid] = texts.get(pid, "") + str(props.get("delta", ""))
    elif kind == "permission.asked":
        s.permissions[props.get("id", "")] = props
        s.push("permission", describe_permission(props), {"id": props.get("id"), "permission": props.get("permission"),
                                                          "patterns": props.get("patterns") or [],
                                                          "metadata": props.get("metadata") or {}})
    elif kind == "permission.replied":
        s.permissions.pop(props.get("requestID", ""), None)
    elif kind == "session.status":
        if (props.get("status") or {}).get("type") == "busy":
            s.busy = True
            s.push("thinking", "thinking")
    elif kind == "session.error":
        err = props.get("error") or {}
        s.push("error", str((err.get("data") or {}).get("message") or err.get("name") or "OpenCode reported an error."))
    elif kind == "session.idle":
        return True
    return False


def handle_tool(s: WebSession, part: dict) -> None:
    state = part.get("state") or {}
    status = state.get("status")
    call_id = part.get("callID") or part.get("id", "")
    name = str(part.get("tool", "")).removeprefix("solidworks_")
    args = state.get("input") or {}
    if status in ("running", "completed", "error") and call_id not in s.tools:
        s.tools[call_id] = "announced"
        note_project(s, name, args)
        data: dict[str, Any] = {"args": args}
        preview = preview_call(name, args)
        if preview:
            data["preview"] = preview
        s.push("tool", name, data)
    if status in ("completed", "error") and s.tools.get(call_id) != "done":
        s.tools[call_id] = "done"
        output = state.get("output") if status == "completed" else json.dumps(
            {"ok": False, "error": "TOOL_ERROR", "message": str(state.get("error", ""))})
        output = output if isinstance(output, str) else json.dumps(output)
        s.push("result", summarize_result(output), {"output": output[:20000]})
        if s.job is not None:
            times = state.get("time") or {}
            seconds = ((times.get("end") or 0) - (times.get("start") or 0)) / 1000
            s.job.step(name, args, output, s.last_model, max(seconds, 0.0))


def describe_permission(props: dict) -> str:
    what = props.get("permission", "something")
    patterns = ", ".join(props.get("patterns") or [])
    return f"OpenCode asks to use {what}" + (f": {patterns}" if patterns else "")


def final_reply(s: WebSession) -> str:
    texts = []
    for mid in s.turn_messages:
        texts.extend(t for t in (s.texts.get(mid) or {}).values() if t.strip())
    return (texts[-1] if texts else "").strip()


async def finish_request(s: WebSession) -> None:
    reply = final_reply(s)
    if reply:
        s.push("reply", reply)
    job, s.job = s.job, None
    s.busy = False
    if job is not None:
        await asyncio.to_thread(save_job, job, reply, s.active_project)
        s.push("job", job.id, {"id": job.id, "changed": job.changed})
    s.push("idle", "")


def save_job(job: jobs.Job, reply: str, project: str | None) -> None:
    try:
        picture = None
        if job.changed:
            folder = jobs.jobs_dir() / job.id
            folder.mkdir(parents=True, exist_ok=True)
            try:
                from sw_mcp.tools.documents import save_picture

                out = json.loads(save_picture(file_path=str(folder / "picture.png")))
                picture = out.get("picture") if out.get("ok") else None
            except Exception:  # noqa: BLE001 - the picture is optional
                log.info("no picture for the job record", exc_info=True)
        design_data = None
        if project:
            from sw_mcp.sw import design

            design_data = design.load(project)
        job.finish(reply, project, design_data, picture)
    except Exception:  # noqa: BLE001 - a record must never break the page
        log.warning("could not save the job record", exc_info=True)


async def pump(s: WebSession) -> None:
    async for ev in s.client.events():
        try:
            if handle_event(s, ev) and s.busy:
                await finish_request(s)
        except Exception:  # noqa: BLE001 - one odd event must not stop the stream
            log.warning("could not handle an OpenCode event: %s", str(ev)[:300], exc_info=True)


# ---------------------------------------------------------------- the app
Backend = Callable[[], Any]  # an async context manager yielding an OpenCodeClient


@asynccontextmanager
async def opencode_backend():
    server = OpenCodeServer(log_file=log_dir() / "opencode-serve.log")
    await asyncio.to_thread(server.start)
    client = server.client()
    try:
        yield client
    finally:
        await client.close()
        await asyncio.to_thread(server.stop)


def create_app(backend: Backend = opencode_backend, token: str | None = None,
               transcript_factory: Callable[[], Any] = lambda: None) -> Starlette:
    token = token or secrets.token_urlsafe(24)
    state: dict[str, WebSession] = {}

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with backend() as client:
            s = WebSession(client=client, transcript=transcript_factory())
            s.session_id = (await client.create_session())["id"]
            state["s"] = s
            task = asyncio.create_task(pump(s))
            try:
                yield
            finally:
                task.cancel()

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
        s.turn_messages, s.texts, s.tools = [], {}, {}
        s.job = jobs.Job(text, "browser")
        s.push("user", text)
        try:
            await s.client.prompt(s.session_id, text, s.model, extra_instructions(s, text))
        except Exception as exc:  # noqa: BLE001 - show it instead of hanging
            s.busy, s.job = False, None
            s.push("error", f"OpenCode did not take the message: {exc}")
            s.push("idle", "")
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return JSONResponse({"ok": True})

    async def events(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        s = state["s"]
        after = int(request.query_params.get("after", "0") or 0)
        if request.query_params.get("wait", "1") != "0" and not any(e["id"] > after for e in s.events):
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
        when, models = s.models_cache
        if time.monotonic() - when > 60 or not models:
            try:
                models = await s.client.models()
                s.models_cache = (time.monotonic(), models)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not list the models: %s", exc)
        return JSONResponse({"models": models, "chosen": s.model, "last_model": s.last_model, "busy": s.busy,
                             "permissions": list(s.permissions.values()), "project": s.active_project})

    async def control(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        s = state["s"]
        body = await request.json()
        action = body.get("action")
        if action == "new":
            if s.busy:
                return JSONResponse({"ok": False, "error": "busy"}, status_code=409)
            s.session_id = (await s.client.create_session())["id"]
            s.active_project, s.roles, s.part_kind = None, {}, {}
            s.push("info", "New conversation.")
        elif action == "stop":
            if not s.busy:
                return JSONResponse({"ok": False, "error": "nothing is running"}, status_code=409)
            await s.client.abort(s.session_id)
            s.push("info", "Stopping...")
        elif action == "use":
            pid, mid = str(body.get("providerID", "")), str(body.get("modelID", ""))
            if not pid or not mid:
                return JSONResponse({"ok": False, "error": "choose a model"}, status_code=400)
            s.model = {"providerID": pid, "modelID": mid}
            s.push("info", f"Using {pid}/{mid} from the next message on.")
        elif action == "auto":
            s.model = None
            s.push("info", "Using the SolidWorks agent's own model.")
        else:
            return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)
        return JSONResponse({"ok": True})

    async def permission(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        s = state["s"]
        body = await request.json()
        rid, reply = str(body.get("id", "")), body.get("reply")
        if rid not in s.permissions or reply not in ("once", "always", "reject"):
            return JSONResponse({"ok": False, "error": "nothing to answer"}, status_code=409)
        await s.client.reply_permission(rid, reply, str(body.get("message", ""))[:500])
        s.permissions.pop(rid, None)
        return JSONResponse({"ok": True})

    async def rate(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        body = await request.json()
        try:
            rating = jobs.rate(str(body.get("id", "")), int(body.get("stars", 0)),
                               [str(t) for t in body.get("tags") or []], str(body.get("comment", "")))
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "unknown job"}, status_code=400)
        return JSONResponse({"ok": True, "rating": rating})

    async def share(request: Request) -> JSONResponse:
        if not authorized(request):
            return denied()
        from .logs import reveal

        path, count = await asyncio.to_thread(jobs.pack)
        if request.query_params.get("open", "1") != "0":
            reveal(path)
        return JSONResponse({"ok": True, "path": str(path), "jobs": count})

    return Starlette(
        routes=[
            Route("/", page),
            Route("/api/send", send, methods=["POST"]),
            Route("/api/events", events),
            Route("/api/status", status),
            Route("/api/control", control, methods=["POST"]),
            Route("/api/permission", permission, methods=["POST"]),
            Route("/api/rate", rate, methods=["POST"]),
            Route("/api/share", share, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def main(open_browser: bool = True, port: int | None = None) -> int:
    import uvicorn

    setup_logging()
    port = port or free_port()
    url = f"http://127.0.0.1:{port}/"
    print("Starting OpenCode in the background, then the page opens in your browser...")
    print(f"SolidWorks Assistant: {url}  (close this window to stop it)")
    if open_browser:
        import threading

        threading.Timer(4.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(transcript_factory=lambda: Transcript("browser")), host="127.0.0.1", port=port,
                log_level="warning")
    return 0
