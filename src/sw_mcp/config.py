"""Runtime settings, read once from environment variables (all optional)."""

from __future__ import annotations

import os
import tempfile


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# Max seconds one tool may spend inside SolidWorks before we give up waiting.
COM_TIMEOUT_S = _float("SW_MCP_COM_TIMEOUT", 30.0)

# Circuit breaker: consecutive connection-level failures before tools fail fast,
# and how long they fail fast before one trial call is allowed again.
BREAKER_THRESHOLD = int(_float("SW_MCP_BREAKER_THRESHOLD", 3))
BREAKER_COOLDOWN_S = _float("SW_MCP_BREAKER_COOLDOWN", 20.0)

# Retries for idempotent tools when SolidWorks rejects a call because it is busy.
BUSY_RETRIES = int(_float("SW_MCP_BUSY_RETRIES", 3))
BUSY_RETRY_DELAY_S = _float("SW_MCP_BUSY_RETRY_DELAY", 1.0)

# get_status may start SolidWorks when no SLDWORKS.exe process exists.
AUTO_LAUNCH = _bool("SW_MCP_AUTO_LAUNCH", True)

# Never log to stdout: stdout is the MCP stdio channel.
LOG_FILE = os.environ.get("SW_MCP_LOG") or os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "sw_mcp", "sw_mcp.log"
)
LOG_LEVEL = os.environ.get("SW_MCP_LOG_LEVEL", "INFO").upper()
