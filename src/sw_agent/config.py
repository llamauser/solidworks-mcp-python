"""The user's non-secret settings: which providers are set up and which models work.

Stored as JSON in %APPDATA%\\sw_agent\\config.json. Keys are NOT stored here (see keys.py).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("SW_AGENT_HOME") or os.path.join(
        os.environ.get("APPDATA") or tempfile.gettempdir(), "sw_agent")
    return Path(base)


@dataclass
class ModelEntry:
    id: str
    tools_ok: bool
    latency_ms: int = 0
    score: float | None = None  # filled by the benchmark (phase 4)
    note: str = ""              # why the tool check failed (shown in the log bundle)


@dataclass
class ProviderEntry:
    enabled: bool = True
    checked: str = ""
    models: list[ModelEntry] = field(default_factory=list)


@dataclass
class UserConfig:
    providers: dict[str, ProviderEntry] = field(default_factory=dict)

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path | None = None) -> "UserConfig":
        path = path or config_dir() / "config.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        cfg = cls()
        for pid, entry in (raw.get("providers") or {}).items():
            models = [ModelEntry(**m) for m in entry.get("models", []) if isinstance(m, dict) and m.get("id")]
            cfg.providers[pid] = ProviderEntry(bool(entry.get("enabled", True)), entry.get("checked", ""), models)
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = path or config_dir() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "providers": {pid: asdict(e) for pid, e in self.providers.items()}}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    # ------------------------------------------------------------ helpers
    def record(self, provider_id: str, models: list[ModelEntry]) -> None:
        self.providers[provider_id] = ProviderEntry(True, date.today().isoformat(), models)

    def remove(self, provider_id: str) -> None:
        self.providers.pop(provider_id, None)

    def working_models(self) -> list[tuple[str, ModelEntry]]:
        """(provider_id, model) pairs that passed the tool check, best first."""
        out = []
        for pid, entry in self.providers.items():
            if entry.enabled:
                out.extend((pid, m) for m in entry.models if m.tools_ok)
        out.sort(key=lambda pm: (-(pm[1].score or 0), pm[1].latency_ms or 10**9))
        return out
