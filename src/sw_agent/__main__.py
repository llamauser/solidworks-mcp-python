"""sw-agent command line.

  sw-agent setup     connect AI providers (opens their key pages, tests and stores keys)
  sw-agent status    show connected providers and working models
  sw-agent connect   use the SolidWorks tools from other apps (VS Code, Claude Desktop, ...)
  sw-agent sync      regenerate the per-app instruction files from src/sw_mcp/guide.md
  sw-agent web       the assistant in your browser
  sw-agent           the assistant in this terminal (same as `sw-agent chat`)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from . import __version__, clients, keys
from .config import UserConfig
from .registry import load_providers

ROOT = Path(__file__).resolve().parents[2]


def cmd_setup(_: argparse.Namespace) -> int:
    from .wizard import Wizard

    Wizard().run()
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    console = Console()
    cfg = UserConfig.load()
    table = Table(title="AI providers")
    for col in ("Provider", "Key", "Working models", "Checked", "Your data"):
        table.add_column(col)
    for p in load_providers():
        entry = cfg.providers.get(p.id)
        models = ", ".join(m.id for m in entry.models if m.tools_ok) if entry else ""
        key = "local" if p.local else keys.mask(keys.get_key(p.id))
        table.add_row(p.name, key, models or "-", entry.checked if entry else "-", p.privacy_label)
    console.print(table)
    if not cfg.working_models():
        console.print("No working models yet. Run [bold]sw-agent setup[/bold].")
    return 0


def cmd_connect(_: argparse.Namespace) -> int:
    console = Console()
    console.print("[bold]Use the SolidWorks tools from other AI apps[/bold]\n")
    console.print("Already set up in this folder (open the folder in the app):")
    console.print("  - OpenCode: run [bold]opencode[/bold] here (agent 'solidworks').")
    console.print("  - VS Code + GitHub Copilot: open this folder, then pick the 'SolidWorks' agent in Chat.")
    console.print("  - Gemini CLI: run [bold]gemini[/bold] here.\n")
    path = clients.claude_desktop_path()
    if Confirm.ask(f"Add the SolidWorks tools to Claude Desktop ({path})?", default=False, console=console):
        try:
            written = clients.add_to_claude_desktop()
            console.print(f"[green]Done.[/green] Restart Claude Desktop. (Backup of the old file kept next to {written}.)")
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
    console.print("\nFor any other app (Cursor, Cline, LM Studio, Windsurf ...), add this to its MCP settings:")
    console.print(clients.generic_snippet(), markup=False, highlight=False)
    return 0


def cmd_sync(_: argparse.Namespace) -> int:
    changed = clients.sync_project_files(ROOT)
    print("updated: " + ", ".join(changed) if changed else "all app files are up to date")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    import asyncio

    from .bench import TASKS, estimate_requests, run_bench
    from .router import Router

    console = Console()
    cfg = UserConfig.load()
    router = Router(cfg)
    models = router.candidates()[: args.models]
    if not models:
        console.print("No working models. Run [bold]sw-agent setup[/bold] first.")
        return 1
    console.print(f"Scoring {len(models)} model(s) on {len(TASKS)} SolidWorks tasks (no SolidWorks needed).")
    console.print(f"This uses about {estimate_requests(len(models))} requests from your free quotas.")
    if not args.yes and not Confirm.ask("Start?", default=True, console=console):
        return 0
    scores = asyncio.run(run_bench(cfg, router, args.models, on_progress=console.print))
    table = Table(title="Benchmark (the router now tries the best first)")
    for col in ("Model", "Score", "Requests", "Note"):
        table.add_column(col)
    for s in scores:
        req = sum(r.requests for r in s.results)
        table.add_row(s.candidate.label, "-" if s.score is None else f"{s.score:.0f}", str(req), s.skipped)
    console.print(table)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web import main as web_main

    return web_main(open_browser=not args.no_browser, port=args.port)


def cmd_chat(_: argparse.Namespace) -> int:
    from .chat import main as chat_main

    return chat_main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sw-agent", description="SolidWorks Assistant")
    parser.add_argument("--version", action="version", version=f"sw-agent {__version__}")
    sub = parser.add_subparsers(dest="command")
    for name, fn, text in (
        ("setup", cmd_setup, "connect AI providers"),
        ("status", cmd_status, "show providers and working models"),
        ("connect", cmd_connect, "use the tools from other AI apps"),
        ("sync", cmd_sync, "regenerate per-app instruction files"),
        ("chat", cmd_chat, "start the assistant"),
        ("bench", cmd_bench, "score connected models on SolidWorks tasks"),
        ("web", cmd_web, "open the assistant in your browser"),
    ):
        p = sub.add_parser(name, help=text)
        p.set_defaults(func=fn)
        if name == "web":
            p.add_argument("--port", type=int, default=None)
            p.add_argument("--no-browser", action="store_true")
        if name == "bench":
            p.add_argument("--models", type=int, default=3, help="how many models to score (best first)")
            p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    return (getattr(args, "func", None) or cmd_chat)(args)


if __name__ == "__main__":
    sys.exit(main())
