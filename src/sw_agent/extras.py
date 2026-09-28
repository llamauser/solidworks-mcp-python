"""Extra abilities offered to OpenAI models only, each switched on by the user:

- internet: web_search (OpenAI's own hosted web search, through its Responses API) and
  fetch_page (reads one web page as plain text);
- terminal: run_command (one PowerShell command on this PC). EVERY command waits for the
  user's OK, whatever the review setting, because a command can change anything on the PC and
  a web page could try to talk the model into one.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
from typing import Any

import httpx

from .llm import ChatClient, LLMError

INTERNET_TOOLS = ("web_search", "fetch_page")
TERMINAL_TOOLS = ("run_command",)
EXTRA_TOOLS = INTERNET_TOOLS + TERMINAL_TOOLS
PAGE_CHARS = 12000
OUTPUT_CHARS = 8000
MAX_PAGE_BYTES = 2_000_000

DEFINITIONS = {
    "web_search": {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the internet and get a short answer with sources. Use for facts you do not know: "
                       "standard sizes (bolts, bearings, O-rings), catalog dimensions, materials.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look up, as a full question."}},
            "required": ["query"]}}},
    "fetch_page": {"type": "function", "function": {
        "name": "fetch_page",
        "description": "Read one web page (http/https) as plain text, e.g. a source that web_search found.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "The full address, starting with https://"}},
            "required": ["url"]}}},
    "run_command": {"type": "function", "function": {
        "name": "run_command",
        "description": "Run ONE PowerShell command on this PC and get its output. The user sees and approves "
                       "every command first. Use only for things outside SolidWorks that the user asked for "
                       "(files and folders, zipping a project, copying exports). Never delete without being asked.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "A PowerShell command."},
            "timeout_s": {"type": "integer", "minimum": 5, "maximum": 300, "default": 60}},
            "required": ["command"]}}},
}


def enabled_tools(settings: dict) -> list[str]:
    names: list[str] = []
    if settings.get("internet"):
        names += INTERNET_TOOLS
    if settings.get("terminal"):
        names += TERMINAL_TOOLS
    return names


def definitions(settings: dict) -> list[dict]:
    return [DEFINITIONS[n] for n in enabled_tools(settings)]


def _fail(code: str, message: str, fix: str = "") -> str:
    out = {"ok": False, "error": code, "message": message}
    if fix:
        out["fix"] = fix
    return json.dumps(out)


# ---------------------------------------------------------------- internet
def _answer_text(body: dict) -> tuple[str, list[dict]]:
    texts, sources = [], []
    for item in body.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                texts.append(part.get("text", ""))
                for ann in part.get("annotations") or []:
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        sources.append({"title": ann.get("title", ""), "url": ann["url"]})
    if not texts and isinstance(body.get("output_text"), str):
        texts.append(body["output_text"])
    unique = list({s["url"]: s for s in sources}.values())
    return "\n".join(texts).strip(), unique[:8]


def web_search(client: ChatClient, model: str, query: str) -> str:
    if not query.strip():
        return _fail("BAD_ARGUMENT", "The search query is empty.")
    last = ""
    for tool in ("web_search", "web_search_preview"):  # the preview name for older accounts/models
        try:
            body = client._request("POST", "/responses", json={
                "model": model, "input": query, "tools": [{"type": tool}]})
        except LLMError as exc:
            last = str(exc)
            if exc.kind == "bad_request":
                continue
            return _fail("SEARCH_FAILED", f"The web search failed: {exc}")
        answer, sources = _answer_text(body)
        if answer:
            return json.dumps({"ok": True, "answer": answer[:PAGE_CHARS], "sources": sources})
        last = "the search returned no text"
    return _fail("SEARCH_FAILED", f"The web search is not available for {model}: {last[:300]}",
                 "Answer from what you know, or ask the user.")


_SCRIPT = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|table)\b[^>]*>", re.I)


def page_text(raw: str) -> str:
    text = _SCRIPT.sub(" ", raw)
    text = _BLOCK.sub("\n", text)
    text = html.unescape(_TAG.sub(" ", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_page(url: str, http: httpx.Client | None = None) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        return _fail("BAD_ARGUMENT", "Only http:// and https:// addresses can be read.")
    own = http is None
    http = http or httpx.Client(timeout=20.0, follow_redirects=True,
                                headers={"User-Agent": "SolidWorks-Assistant/1.0"})
    try:
        with http.stream("GET", url) as resp:
            if resp.status_code >= 400:
                return _fail("FETCH_FAILED", f"The page answered {resp.status_code}.")
            data = b""
            for chunk in resp.iter_bytes():
                data += chunk
                if len(data) > MAX_PAGE_BYTES:
                    break
            kind = resp.headers.get("content-type", "")
            encoding = resp.encoding or "utf-8"
    except httpx.HTTPError as exc:
        return _fail("FETCH_FAILED", f"Could not read {url}: {exc}")
    finally:
        if own:
            http.close()
    raw = data.decode(encoding, errors="replace")
    text = page_text(raw) if "html" in kind or raw.lstrip().startswith("<") else raw
    return json.dumps({"ok": True, "url": url, "text": text[:PAGE_CHARS],
                       "cut": len(text) > PAGE_CHARS})


# ---------------------------------------------------------------- terminal
def working_folder() -> str:
    from sw_mcp.sw.project import projects_root

    folder = projects_root()
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder)


def run_command(command: str, timeout_s: int = 60, runner: Any = subprocess.run) -> str:
    command = command.strip()
    if not command:
        return _fail("BAD_ARGUMENT", "The command is empty.")
    timeout_s = max(5, min(int(timeout_s or 60), 300))
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("SW_AGENT_KEY_")}
    script = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + command
    try:
        done = runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                      cwd=working_folder(), env=env, capture_output=True, text=True, encoding="utf-8",
                      errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return _fail("TIMEOUT", f"The command did not finish within {timeout_s} s and was stopped.")
    except OSError as exc:
        return _fail("COMMAND_FAILED", f"Could not start PowerShell: {exc}")
    output = ((done.stdout or "") + (("\n" + done.stderr) if done.stderr else "")).strip()
    return json.dumps({"ok": done.returncode == 0, "exit_code": done.returncode,
                       "output": output[-OUTPUT_CHARS:], "cut": len(output) > OUTPUT_CHARS,
                       "folder": working_folder()})
