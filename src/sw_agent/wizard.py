"""The setup wizard: connect AI providers one by one, in plain language.

For each provider it explains what you get and what happens to your data, opens the page
where you create a key, takes the key (hidden while you type), checks it live, finds models
that can use tools, and stores the key in Windows Credential Manager.
"""

from __future__ import annotations

import webbrowser
from typing import Callable

import logging

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from . import keys
from .config import ModelEntry, UserConfig
from .probe import Discovery, discover
from .registry import Provider, load_providers

log = logging.getLogger("sw_agent.setup")

PRIVACY_STYLE = {"no-training": "green", "local": "green", "depends": "yellow", "may-train": "bold red"}
MAX_KEY_TRIES = 3


class Wizard:
    def __init__(
        self,
        console: Console | None = None,
        ask: Callable[..., str] | None = None,
        open_url: Callable[[str], object] = webbrowser.open,
        discover_fn: Callable[..., Discovery] = discover,
        providers: list[Provider] | None = None,
        config: UserConfig | None = None,
    ) -> None:
        self.console = console or Console()
        self.ask = ask or (lambda prompt, password=False, default="": Prompt.ask(
            prompt, password=password, default=default, show_default=False, console=self.console))
        self.open_url = open_url
        self.discover_fn = discover_fn
        self.providers = providers if providers is not None else load_providers()
        self.config = config if config is not None else UserConfig.load()

    # ------------------------------------------------------------ main flow
    def run(self) -> UserConfig:
        c = self.console
        c.print(Panel.fit(
            "[bold]Connect AI models to the SolidWorks Assistant[/bold]\n\n"
            "Each provider below gives some free usage. For each one you want:\n"
            "  1. a web page opens where you sign in and create a key,\n"
            "  2. you copy the key and paste it here (it stays hidden),\n"
            "  3. the key is tested and saved in Windows Credential Manager (not in a file).\n\n"
            "More providers = the assistant keeps working when one hits its daily limit.\n"
            "You can skip any provider and run this again later: [bold]sw-agent setup[/bold]",
            title="Setup", border_style="cyan"))
        for provider in self.providers:
            action = self._offer(provider)
            if action == "quit":
                break
        self.config.save()
        self.summary()
        return self.config

    def _offer(self, p: Provider) -> str:
        c = self.console
        style = PRIVACY_STYLE.get(p.privacy, "white")
        existing = self.config.providers.get(p.id)
        status = ""
        if existing and existing.models:
            ok = sum(m.tools_ok for m in existing.models)
            status = f"\n[green]Already set up[/green]: {ok} working model(s), checked {existing.checked}."
        c.print(Panel(
            f"{p.offer}\n[dim]Needs:[/dim] {p.requires}\n"
            f"[dim]Your data:[/dim] [{style}]{p.privacy_label}[/{style}]. {p.privacy_note}{status}",
            title=f"[bold]{p.name}[/bold]", border_style=style))
        if p.privacy == "may-train":
            c.print("[red]Only use this provider for designs you are allowed to share.[/red]")
        if existing and existing.models:
            choice = self._choose("[Enter] keep  [t] test again  [r] replace key  [d] remove  [q] finish",
                                  {"": "keep", "t": "test", "r": "setup", "d": "remove", "q": "quit"})
        else:
            choice = self._choose("[Enter] set up  [s] skip  [q] finish",
                                  {"": "setup", "s": "skip", "q": "quit"})
        if choice == "remove":
            keys.delete_key(p.id)
            self.config.remove(p.id)
            c.print(f"Removed {p.name}.")
        elif choice == "test":
            self._check_and_save(p, keys.get_key(p.id))
        elif choice == "setup":
            self._setup(p)
        return choice

    def _choose(self, prompt: str, options: dict[str, str]) -> str:
        while True:
            answer = self.ask(prompt).strip().lower()
            if answer in options:
                return options[answer]
            self.console.print(f"Please type one of: {', '.join(repr(k) if k else 'Enter' for k in options)}")

    def _setup(self, p: Provider) -> None:
        c = self.console
        if p.local:
            c.print(f"Looking for {p.name} on this PC...")
            self._check_and_save(p, None)
            return
        c.print(f"Opening [link={p.key_url}]{p.key_url}[/link] in your browser. Create a key there and copy it.")
        try:
            self.open_url(p.key_url)
        except Exception:  # noqa: BLE001 - the link is printed anyway
            pass
        for attempt in range(1, MAX_KEY_TRIES + 1):
            key = self.ask("Paste the key here (hidden), or just press Enter to skip", password=True).strip()
            if not key:
                c.print("Skipped.")
                return
            if self._check_and_save(p, key):
                return
            if attempt < MAX_KEY_TRIES:
                c.print("Let's try again. Make sure you copied the whole key.")

    def _check_and_save(self, p: Provider, key: str | None) -> bool:
        c = self.console
        if not p.local and not key:
            c.print("[red]No key stored for this provider.[/red]")
            return False
        with c.status(f"Checking {p.name} and testing which models can use tools..."):
            found = self.discover_fn(p, key)
        log.info("setup %s: key_ok=%s models=%s error=%s checks=%s", p.id, found.key_ok, found.model_count,
                 found.error[:200], [(ch.model, ch.tools_ok, ch.detail[:80]) for ch in found.checks])
        if not found.key_ok:
            if p.local:
                c.print(f"[yellow]{p.name} is not running.[/yellow] {p.requires}")
            else:
                c.print(f"[red]That key did not work:[/red] {found.error}")
            return False
        if key and not p.local:
            keys.set_key(p.id, key)
        entries = [ModelEntry(ch.model, ch.tools_ok, ch.latency_ms) for ch in found.checks]
        self.config.record(p.id, entries)
        self.config.save()
        table = Table(title=f"{p.name}: {found.model_count} models listed", show_lines=False)
        table.add_column("Model")
        table.add_column("Can use tools")
        table.add_column("Answer time")
        for ch in found.checks:
            table.add_row(ch.model, "[green]yes[/green]" if ch.tools_ok else f"[red]no[/red] ({ch.detail})",
                          f"{ch.latency_ms / 1000:.1f} s" if ch.latency_ms else "-")
        c.print(table)
        if found.working:
            c.print(f"[green]Saved.[/green] {len(found.working)} model(s) ready.")
        else:
            c.print(f"[yellow]The key works, but no tested model could use tools right now.[/yellow] {found.error}"
                    " The key is saved; run [bold]sw-agent setup[/bold] later to test again.")
        return True

    # ------------------------------------------------------------ summary
    def summary(self) -> None:
        c = self.console
        working = self.config.working_models()
        if not working:
            c.print(Panel("No working models yet. Run [bold]sw-agent setup[/bold] again and connect at least "
                          "one provider.", border_style="red"))
            return
        table = Table(title="Models the assistant can use (best first)")
        table.add_column("Provider")
        table.add_column("Model")
        table.add_column("Answer time")
        names = {p.id: p.name for p in self.providers}
        for pid, m in working:
            table.add_row(names.get(pid, pid), m.id, f"{m.latency_ms / 1000:.1f} s")
        c.print(table)
        c.print("Next: open SolidWorks, then run [bold]sw-agent[/bold] to start the assistant.")
