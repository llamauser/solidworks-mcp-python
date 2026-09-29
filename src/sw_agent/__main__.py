"""sw-agent command line.

  sw-agent setup     connect AI providers (opens their key pages, tests and stores keys)
  sw-agent status    show connected providers and working models
  sw-agent connect   use the SolidWorks tools from other apps (VS Code, Claude Desktop, ...)
  sw-agent sync      regenerate the per-app instruction files from src/sw_mcp/guide.md
  sw-agent web       the assistant in your browser
  sw-agent logs      zip the logs onto the Desktop to send to the developer
  sw-agent check     SolidWorks end-to-end check (the smoke test), then zip the logs
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


def cmd_setup(args: argparse.Namespace) -> int:
    from .logs import setup_logging
    from .wizard import Wizard

    setup_logging()
    Wizard().run(only=getattr(args, "provider", "") or "")
    return 0


def _hand_over_logs(console: Console, open_explorer: bool = True) -> None:
    from .logs import collect, log_dir, reveal

    path = collect()
    console.print(f"\n[bold green]Logs saved:[/bold green] {path}")
    if open_explorer:
        reveal(path)
        console.print("A File Explorer window opened with the zip selected (its path is also copied).")
        console.print("Drag that zip into the chat with the developer.")
    console.print("It has the logs, your recent conversations and the test results. It does NOT contain "
                  "your API keys.")
    console.print(f"[dim]Raw logs folder: {log_dir()}[/dim]")


def cmd_share(args: argparse.Namespace) -> int:
    from . import jobs
    from .logs import reveal

    console = Console()
    path, count = jobs.pack()
    console.print(f"\n[bold green]Packed {count} build record(s):[/bold green] {path}")
    console.print("Send this file to the developer (for example upload it to the shared Google Drive folder).")
    console.print("[dim]It contains your requests, the steps, the results, pictures and ratings. "
                  "No API keys, file paths or user names.[/dim]")
    if not getattr(args, "no_open", False):
        reveal(path)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    _hand_over_logs(Console(), open_explorer=not getattr(args, "no_open", False))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Run the SolidWorks end-to-end check (scripts/smoke_test.py), then pack the logs."""
    import subprocess

    console = Console()
    script = ROOT / "scripts" / "smoke_test.py"
    if not script.exists():
        console.print(f"[red]Cannot find {script}.[/red] Re-download the project.")
        return 1
    console.print("[bold]Checking SolidWorks[/bold] (it starts SolidWorks if needed and builds test parts "
                  "in a temp folder; your files are not touched). This takes 1-3 minutes.\n")
    cmd = [sys.executable, str(script)] + (["--part", args.part] if args.part else [])
    code = subprocess.call(cmd, cwd=str(ROOT))
    _hand_over_logs(console, open_explorer=not args.no_open)
    return code


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

    from .logs import setup_logging

    setup_logging()
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
        ("logs", cmd_logs, "collect logs into a zip on the Desktop (no keys)"),
        ("share", cmd_share, "pack your build records and ratings into a zip for the developer"),
        ("check", cmd_check, "check SolidWorks end to end, then collect the logs"),
    ):
        p = sub.add_parser(name, help=text)
        p.set_defaults(func=fn)
        if name == "setup":
            p.add_argument("provider", nargs="?", default="", help='set up only this provider, e.g. "openai"')
        if name in ("logs", "check", "share"):
            p.add_argument("--no-open", action="store_true", help="do not open File Explorer")
        if name == "check":
            p.add_argument("--part", default=None, help="test with a copy of this part instead of a test block")
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
