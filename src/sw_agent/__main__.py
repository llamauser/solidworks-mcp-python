"""sw-agent command line (the SolidWorks Assistant around OpenCode).

  sw-agent web       the assistant in your browser (starts OpenCode in the background) [default]
  sw-agent cli       OpenCode in this terminal, with the SolidWorks agent
  sw-agent connect   connect an AI provider in OpenCode (optional: OpenCode's free models need no key)
  sw-agent models    list the AI models OpenCode can use
  sw-agent status    OpenCode and SolidWorks tool status
  sw-agent check     SolidWorks end-to-end check (the smoke test), then zip the logs
  sw-agent logs      zip the logs onto the Desktop to send to the developer
  sw-agent share     zip your build records and ratings onto the Desktop
  sw-agent sync      regenerate the OpenCode agent file from src/sw_mcp/guide.md
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from . import __version__, agentfile, opencode

ROOT = Path(__file__).resolve().parents[2]


def _opencode_or_explain(console: Console) -> str | None:
    exe = opencode.find_opencode()
    if not exe:
        console.print("[red]OpenCode is not installed.[/red] Double-click 'Install SolidWorks Assistant.bat' "
                      "(or Tools menu, option 8) to install it.")
    return exe


def cmd_web(args: argparse.Namespace) -> int:
    if not _opencode_or_explain(Console()):
        return 1
    agentfile.sync(ROOT)
    from .web import main as web_main

    return web_main(open_browser=not getattr(args, "no_browser", False), port=getattr(args, "port", None))


def cmd_cli(_: argparse.Namespace) -> int:
    if not _opencode_or_explain(Console()):
        return 1
    agentfile.sync(ROOT)
    return opencode.run_tui()


def cmd_connect(_: argparse.Namespace) -> int:
    console = Console()
    exe = _opencode_or_explain(console)
    if not exe:
        return 1
    console.print("OpenCode's own free models work without a key. To add another provider (OpenRouter, "
                  "OpenAI, Gemini, Groq ...), pick it in the list and paste its key.\n")
    return subprocess.call([exe, "providers", "login"], cwd=ROOT)


def cmd_models(_: argparse.Namespace) -> int:
    exe = _opencode_or_explain(Console())
    return subprocess.call([exe, "models"], cwd=ROOT) if exe else 1


def cmd_status(_: argparse.Namespace) -> int:
    console = Console()
    exe = _opencode_or_explain(console)
    if exe:
        console.print(f"OpenCode {opencode.version(exe)} at {exe}")
        subprocess.call([exe, "mcp", "list"], cwd=ROOT)
        subprocess.call([exe, "providers", "list"], cwd=ROOT)
    console.print(f"SolidWorks Assistant {__version__}, Python {sys.version.split()[0]}, project {ROOT}")
    return 0 if exe else 1


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


def cmd_logs(args: argparse.Namespace) -> int:
    _hand_over_logs(Console(), open_explorer=not getattr(args, "no_open", False))
    return 0


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


def cmd_check(args: argparse.Namespace) -> int:
    """Run the SolidWorks end-to-end check (scripts/smoke_test.py), then pack the logs."""
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


def cmd_sync(_: argparse.Namespace) -> int:
    print("updated: " + agentfile.AGENT_FILE if agentfile.sync(ROOT) else "the agent file is up to date")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sw-agent", description="SolidWorks Assistant (with OpenCode)")
    parser.add_argument("--version", action="version", version=f"sw-agent {__version__}")
    sub = parser.add_subparsers(dest="command")
    for name, fn, text in (
        ("web", cmd_web, "the assistant in your browser (default)"),
        ("cli", cmd_cli, "OpenCode in this terminal with the SolidWorks agent"),
        ("chat", cmd_cli, "same as cli"),
        ("connect", cmd_connect, "connect an AI provider in OpenCode"),
        ("models", cmd_models, "list the AI models OpenCode can use"),
        ("status", cmd_status, "OpenCode and SolidWorks tool status"),
        ("check", cmd_check, "check SolidWorks end to end, then collect the logs"),
        ("logs", cmd_logs, "collect logs into a zip on the Desktop (no keys)"),
        ("share", cmd_share, "pack your build records and ratings into a zip for the developer"),
        ("sync", cmd_sync, "regenerate the OpenCode agent file from the guide"),
    ):
        p = sub.add_parser(name, help=text)
        p.set_defaults(func=fn)
        if name in ("logs", "check", "share"):
            p.add_argument("--no-open", action="store_true", help="do not open File Explorer")
        if name == "check":
            p.add_argument("--part", default=None, help="test with a copy of this part instead of a test block")
        if name == "web":
            p.add_argument("--port", type=int, default=None)
            p.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    return (getattr(args, "func", None) or cmd_web)(args)


if __name__ == "__main__":
    sys.exit(main())
