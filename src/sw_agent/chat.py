"""The terminal version of the SolidWorks Assistant (`sw-agent` / `sw-agent chat`)."""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .assistant import LEAN_TOOLS, Assistant, Event, Toolbox
from .config import UserConfig
from .router import Router

HELP = """[bold]Just type what you want[/bold], for example:
  make a 80 x 50 x 8 plate with 4 holes of 6 mm, 10 mm from the edges
  (click a face in SolidWorks) make this 3 mm thicker
  save it as C:\\Parts\\plate.SLDPRT and export a STEP next to it

Commands:
  /models        which AI models are connected and ready
  /use N         always try model number N first     /auto   go back to automatic choice
  /new           start a new conversation (saves requests on long sessions)
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
    config = UserConfig.load()
    if not config.working_models():
        console.print(Panel("No AI model is connected yet.\nRun [bold]sw-agent setup[/bold] first "
                            "(it takes a few minutes and is free).", border_style="red"))
        return 1
    router = Router(config)
    console.print(Panel.fit("[bold]SolidWorks Assistant[/bold]\nType what you want to build or change. "
                            "[dim]/help for commands.[/dim]", border_style="cyan"))
    tool_names: tuple[str, ...] | None = LEAN_TOOLS
    while True:
        async with Toolbox(tool_names) as toolbox:
            assistant = Assistant(router, toolbox, on_event=lambda e: render(console, e))
            switch = await _loop(console, router, assistant)
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
