"""sw-agent command line.

  sw-agent setup     connect AI providers (opens their key pages, tests and stores keys)
  sw-agent status    show connected providers and working models
  sw-agent connect   use the SolidWorks tools from other apps (VS Code, Claude Desktop, ...)
  sw-agent sync      regenerate the per-app instruction files from src/sw_mcp/guide.md
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


def cmd_chat(_: argparse.Namespace) -> int:
    Console().print("The built-in chat assistant arrives in the next update. For now use OpenCode, VS Code "
                    "Copilot or another app: run [bold]sw-agent connect[/bold] to see how.")
    return 0


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
    ):
        sub.add_parser(name, help=text).set_defaults(func=fn)
    args = parser.parse_args(argv)
    return (getattr(args, "func", None) or cmd_chat)(args)


if __name__ == "__main__":
    sys.exit(main())
