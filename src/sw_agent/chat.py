"""The terminal version of the SolidWorks Assistant (`sw-agent` / `sw-agent chat`)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .assistant import LEAN_TOOLS, REVIEW_TOOLS, Assistant, Decision, Event, PendingCall, Toolbox
from .config import UserConfig
from .logs import Transcript, setup_logging
from .review import edited_args, preview_call
from .router import Router

HELP = """[bold]Just type what you want[/bold], for example:
  make a 80 x 50 x 8 plate with 4 holes of 6 mm, 10 mm from the edges
  (click a face in SolidWorks) make this 3 mm thicker
  save it as C:\\Parts\\plate.SLDPRT and export a STEP next to it

Commands:
  /models        which AI models are connected and ready
  /use N         always try model number N first     /auto   go back to automatic choice
  /new           start a new conversation (saves requests on long sessions)
  /review builds show each plan before building it (run, edit, skip or stop)
  /review all    ask before every change        /review off   just build
  /context       what the AI receives with the next request
  /tools full    offer every tool to the model       /tools lean   the smaller default set
  /quit          leave
"""

STYLE = {"thinking": "dim", "tool": "cyan", "result": "green", "model": "yellow", "error": "bold red"}


def render(console: Console, event: Event) -> None:
    if event.kind == "reply":
        return
    if event.kind == "thinking":
        console.print("  [dim]... thinking[/dim]")
    elif event.kind == "tool":
        console.print(f"  [cyan]-> {event.text}[/cyan]")
    elif event.kind == "result":
        style = "red" if event.text.startswith("problem") else "green"
        console.print(f"     [{style}]{event.text}[/{style}]")
    else:
        console.print(f"  [{STYLE.get(event.kind, 'white')}]{event.text}[/]")


def preview_table(preview: dict | None) -> Table | str:
    if not preview:
        return ""
    if preview.get("ok") is False:
        return f"[red]problem: {preview.get('message', '')}[/red]\n[dim]{preview.get('fix', '')}[/dim]"
    table = Table(show_header=True, header_style="bold")
    if "steps" in preview:
        table.add_column("#")
        table.add_column("")
        table.add_column("Step")
        for step in preview["steps"]:
            style = "red" if step["mode"] == "cut" else "green"
            table.add_row(str(step["n"]), f"[{style}]{step['mode']}[/{style}]", step["what"])
        return table
    table.add_column("")
    table.add_column("")
    for key, value in preview.items():
        if key != "ok" and value is not None:
            table.add_row(key.replace("_", " "), ", ".join(value) if isinstance(value, list) else str(value))
    return table


def _pretty_args(args: dict) -> str:
    copy = dict(args)
    if isinstance(copy.get("plan"), str):
        try:
            copy["plan"] = json.loads(copy["plan"])
        except ValueError:
            pass
    return json.dumps(copy, indent=2)


def edit_in_notepad(text: str) -> str:
    """Let the user edit text in Notepad (waits until Notepad is closed)."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="sw_plan_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        subprocess.run(["notepad.exe", path], check=False)
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        os.remove(path)


def make_approver(console: Console, editor=edit_in_notepad, ask=None):
    ask = ask or (lambda prompt: console.input(prompt))

    async def approve(call: PendingCall) -> Decision:
        args = call.args
        console.print(Panel.fit(f"[bold]Waiting for you:[/bold] {call.name}"
                                + (f"\n[dim]{call.reason}[/dim]" if call.reason else ""), border_style="yellow"))
        console.print(preview_table(preview_call(call.name, args)))
        while True:
            choice = (await asyncio.to_thread(
                ask, "[bold]Enter[/bold]=run  [bold]e[/bold]=edit  [bold]s[/bold]=skip with a note  "
                     "[bold]v[/bold]=view JSON  [bold]q[/bold]=stop > ")).strip().lower()
            if choice in ("", "r", "run", "y", "yes"):
                return Decision("run", args)
            if choice in ("q", "stop"):
                return Decision("stop")
            if choice in ("v", "view"):
                console.print(_pretty_args(args), markup=False, highlight=False)
            elif choice in ("s", "skip"):
                note = await asyncio.to_thread(ask, "What should the AI do instead? > ")
                return Decision("skip", note=note.strip())
            elif choice in ("e", "edit"):
                text = await asyncio.to_thread(editor, _pretty_args(args))
                try:
                    args = edited_args(args, json.loads(text))
                except ValueError as exc:
                    console.print(f"[red]That is not valid JSON ({exc}); nothing changed.[/red]")
                    continue
                console.print("[cyan]Your edited version:[/cyan]")
                console.print(preview_table(preview_call(call.name, args)))

    return approve


def show_context(console: Console, assistant: Assistant) -> None:
    ctx = assistant.context()
    console.print(f"About [bold]{ctx['estimated_tokens']}[/bold] tokens per request; last model: "
                  f"{ctx['model'] or '-'}; review: {ctx['review']}")
    for msg in ctx["messages"][1:]:
        who = "tool result" if msg["role"] == "tool" else msg["role"]
        text = msg.get("content") or ""
        for c in msg.get("tool_calls") or []:
            text += f"\ncall {c['function']['name']} {c['function']['arguments'][:300]}"
        console.print(f"[bold]{who}:[/bold] {text[:600]}", markup=True, highlight=False)


def models_table(router: Router) -> Table:
    table = Table(title="Connected models (tried top to bottom)")
    table.add_column("#")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("State")
    for i, (prov, model, state) in enumerate(router.status(), 1):
        table.add_row(str(i), prov, model, state)
    return table


async def run_chat(console: Console | None = None) -> int:
    console = console or Console()
    setup_logging()
    config = UserConfig.load()
    if not config.working_models():
        console.print(Panel("No AI model is connected yet.\nRun [bold]sw-agent setup[/bold] first "
                            "(it takes a few minutes and is free).", border_style="red"))
        return 1
    router = Router(config)
    console.print(Panel.fit("[bold]SolidWorks Assistant[/bold]\nType what you want to build or change. "
                            "[dim]/help for commands.[/dim]", border_style="cyan"))
    tool_names: tuple[str, ...] | None = LEAN_TOOLS
    review = "off"
    while True:
        async with Toolbox(tool_names) as toolbox:
            assistant = Assistant(router, toolbox, on_event=lambda e: render(console, e),
                                  transcript=Transcript("terminal"), approver=make_approver(console), review=review)
            switch = await _loop(console, router, assistant)
            review = assistant.review
        if switch == "quit":
            return 0
        tool_names = None if switch == "full" else LEAN_TOOLS
        console.print(f"[dim]Tool set: {switch}. Conversation restarted.[/dim]")


async def _loop(console: Console, router: Router, assistant: Assistant) -> str:
    while True:
        try:
            text = (await asyncio.to_thread(console.input, "\n[bold]you>[/bold] ")).strip()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if not text:
            continue
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            cmd, arg = cmd.lower(), arg.strip()
            if cmd in ("quit", "exit", "q"):
                return "quit"
            if cmd == "help":
                console.print(HELP)
            elif cmd == "models":
                console.print(models_table(router))
            elif cmd == "use" and arg.isdigit():
                cands = router.candidates()
                n = int(arg)
                if 1 <= n <= len(cands):
                    router.pin(cands[n - 1].provider.id, cands[n - 1].model)
                    console.print(f"Using {cands[n - 1].label} first.")
                else:
                    console.print("No model with that number. Type /models.")
            elif cmd == "auto":
                router.unpin()
                console.print("Automatic model choice.")
            elif cmd == "new":
                assistant.reset()
                console.print("New conversation.")
            elif cmd == "tools" and arg in ("full", "lean"):
                return arg
            elif cmd == "review" and arg in REVIEW_TOOLS:
                assistant.review = arg
                console.print({"off": "Review off: I build without asking.",
                               "builds": "I will show you each plan before building it.",
                               "all": "I will ask before every change in SolidWorks."}[arg])
            elif cmd == "context":
                show_context(console, assistant)
            else:
                console.print("Unknown command. Type /help.")
            continue
        try:
            reply = await assistant.send(text)
        except KeyboardInterrupt:
            console.print("[yellow]Stopped.[/yellow]")
            continue
        console.print(Panel(Markdown(reply), border_style="cyan", title="assistant", title_align="left"))


def main() -> int:
    try:
        return asyncio.run(run_chat())
    except KeyboardInterrupt:
        return 0
