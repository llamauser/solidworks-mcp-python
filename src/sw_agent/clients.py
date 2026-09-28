"""Connect the SolidWorks tools to other AI apps (any MCP client).

Project files (committed in the repo, generated from src/sw_mcp/guide.md by `sw-agent sync`):
  .opencode/agents/solidworks.md        OpenCode agent (only the SolidWorks tools allowed)
  .github/agents/solidworks.agent.md    VS Code Copilot custom agent
  GEMINI.md                             Gemini CLI project instructions
  .vscode/mcp.json, .gemini/settings.json, opencode.json   server entries (static)

App-wide configs (written only when the user confirms, with a backup):
  Claude Desktop  %APPDATA%\\Claude\\claude_desktop_config.json
Other apps (Cursor, Cline, LM Studio, Windsurf ...) use the common "mcpServers" snippet.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from importlib import resources
from pathlib import Path

OPENCODE_FRONTMATTER = """---
description: Builds, inspects and edits SolidWorks parts using only the solidworks tools
mode: primary
model: openrouter/nvidia/nemotron-3-super-120b-a12b:free
temperature: 0.1
steps: 40
permission:
  bash: deny
  edit: deny
  read: deny
  glob: deny
  grep: deny
  list: deny
  lsp: deny
  task: deny
  skill: deny
  todoread: deny
  todowrite: deny
  question: deny
  webfetch: deny
  websearch: deny
  codesearch: deny
  solidworks_*: allow
---
"""

COPILOT_FRONTMATTER = """---
name: SolidWorks
description: Builds, inspects and edits SolidWorks parts using only the solidworks tools
tools: ['solidworks/*']
---
"""

GENERATED_NOTE = "<!-- Generated from src/sw_mcp/guide.md by `sw-agent sync`. Edit the guide, not this file. -->\n"


def guide_text() -> str:
    return resources.files("sw_mcp").joinpath("guide.md").read_text(encoding="utf-8")


def render_project_files() -> dict[str, str]:
    """Relative path -> content for every generated client file."""
    guide = guide_text()
    return {
        ".opencode/agents/solidworks.md": OPENCODE_FRONTMATTER + "\n" + guide,
        ".github/agents/solidworks.agent.md": COPILOT_FRONTMATTER + "\n" + GENERATED_NOTE + "\n" + guide,
        "GEMINI.md": "# SolidWorks assistant\n\n" + GENERATED_NOTE + "\n" + guide,
    }


def sync_project_files(root: Path) -> list[str]:
    changed = []
    for rel, content in render_project_files().items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            changed.append(rel)
    return changed


def server_entry(python: str | None = None) -> dict:
    """The common MCP server entry, with an absolute Python path."""
    return {
        "command": python or sys.executable,
        "args": ["-m", "sw_mcp"],
        "env": {"SW_MCP_INSTRUCTIONS": "1"},
    }


def generic_snippet(python: str | None = None) -> str:
    return json.dumps({"mcpServers": {"solidworks": server_entry(python)}}, indent=2)


def claude_desktop_path() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"


def add_to_claude_desktop(python: str | None = None, path: Path | None = None) -> Path:
    """Merge our server into Claude Desktop's config, keeping a backup of the old file."""
    path = path or claude_desktop_path()
    data: dict = {}
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise ValueError(f"{path} is not valid JSON; fix or delete it first.") from exc
    data.setdefault("mcpServers", {})["solidworks"] = server_entry(python)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path
