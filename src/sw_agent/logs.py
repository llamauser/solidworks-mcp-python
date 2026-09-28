"""Logging for the assistant, and `sw-agent logs` to collect everything for a bug report.

- Technical log: the same file as the server (%LOCALAPPDATA%\\sw_mcp\\sw_mcp.log): every tool call,
  every model request (provider, model, time, tokens) and every error.
- Conversations: %LOCALAPPDATA%\\sw_mcp\\conversations\\<date>_<time>.jsonl, one line per event
  (what you typed, each step, each answer). Kept on this PC only.
- `sw-agent logs` zips the logs, recent conversations, the provider test results and the
  smoke test report onto the Desktop. API keys are never included (they are not in any file).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

from sw_mcp import config as sw_config

log = logging.getLogger("sw_agent")
_configured = False


def log_dir() -> Path:
    return Path(sw_config.LOG_FILE).parent


def setup_logging() -> None:
    """File logging for sw-agent (idempotent). The console only shows what the UI prints."""
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(sw_config.LOG_FILE, maxBytes=2_000_000, backupCount=2,
                                                       encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError:
        pass
    # keep library chatter (HTTP requests, MCP internals) out of the log
    for noisy in ("httpx", "httpcore", "mcp", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log.info("sw-agent started (python %s, %s)", platform.python_version(), platform.platform())


class Transcript:
    """Writes every assistant event of one conversation to a .jsonl file."""

    def __init__(self, interface: str) -> None:
        folder = log_dir() / "conversations"
        self.path: Path | None = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / f"{datetime.now():%Y-%m-%d_%H%M%S}_{interface}.jsonl"
        except OSError:
            self.path = None

    def write(self, kind: str, text: str, **extra) -> None:
        if self.path is None:
            return
        record = {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "text": text, **extra}
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def collect(dest_folder: Path | None = None, max_conversations: int = 20) -> Path:
    """Zip everything useful for a bug report. Returns the zip path."""
    desktop = Path(os.path.expanduser("~")) / "Desktop"
    dest_folder = dest_folder or (desktop if desktop.exists() else Path.cwd())
    zip_path = dest_folder / f"SolidWorks Assistant logs {datetime.now():%Y-%m-%d %H%M}.zip"
    project_root = Path(__file__).resolve().parents[2]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(log_dir().glob("sw_mcp.log*")):
            zf.write(f, f"logs/{f.name}")
        convs = sorted((log_dir() / "conversations").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        for f in convs[-max_conversations:]:
            zf.write(f, f"conversations/{f.name}")
        cfg = Path(os.environ.get("SW_AGENT_HOME") or Path(os.environ.get("APPDATA", "")) / "sw_agent") / "config.json"
        if cfg.exists():
            zf.write(cfg, "provider_tests.json")  # models and scores only; keys are not in this file
        report = project_root / "smoke_test_report.txt"
        if report.exists():
            zf.write(report, "smoke_test_report.txt")
        zf.writestr("system.txt", "\n".join([
            f"collected: {datetime.now().isoformat(timespec='seconds')}",
            f"python: {sys.version}",
            f"platform: {platform.platform()}",
            f"project: {project_root}",
            f"timezone offset: {time.strftime('%z')}",
        ]))
    return zip_path
