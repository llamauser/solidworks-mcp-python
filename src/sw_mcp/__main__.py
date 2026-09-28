"""Entry point: `python -m sw_mcp` (stdio, for OpenCode) or `python -m sw_mcp --http`."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys

from . import __version__, config


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            config.LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass
    err = logging.StreamHandler(sys.stderr)  # never stdout: it carries the MCP protocol
    err.setLevel(logging.WARNING)
    err.setFormatter(fmt)
    root.addHandler(err)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sw_mcp", description="SolidWorks MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--version", action="version", version=f"sw_mcp {__version__}")
    args = parser.parse_args()

    _setup_logging()
    logging.getLogger("sw_mcp").info("starting sw_mcp %s (%s)", __version__, "http" if args.http else "stdio")

    from .server import mcp

    if args.http:
        mcp.run("streamable-http", host=args.host, port=args.port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
