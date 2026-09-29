"""The OpenCode agent file, generated from the one operating guide (src/sw_mcp/guide.md).

`.opencode/agents/solidworks.md` = the frontmatter below (model, permissions: only the SolidWorks
tools, terminal commands and file reading ask first) + the guide. `sw-agent sync` rewrites it;
edit the guide or this frontmatter, not the generated file.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

AGENT_FILE = ".opencode/agents/solidworks.md"

FRONTMATTER = """---
description: Builds, inspects and edits SolidWorks parts using only the solidworks tools
mode: primary
model: opencode/nemotron-3-ultra-free
temperature: 0.1
steps: 40
permission:
  bash: ask
  edit: deny
  read: ask
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


def guide_text() -> str:
    return resources.files("sw_mcp").joinpath("guide.md").read_text(encoding="utf-8")


def render() -> str:
    return FRONTMATTER + "\n" + guide_text()


def sync(root: Path) -> bool:
    """Write the agent file if it changed. Returns True when it was written."""
    path = root / AGENT_FILE
    content = render()
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True
