"""FastMCP entry point.

This module is intentionally tiny: it builds the FastMCP instance, hands it
to ``tools.register_all`` and starts the stdio transport. Adding a new tool
does **not** require editing this file — drop the new module into
``qlik_lineage_mcp/tools/`` and ``register_all`` will pick it up.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from .tools import register_all


def build_server() -> FastMCP:
    """Construct a FastMCP server with every discovered tool registered.

    Kept as a function so tests can build an isolated server instance and
    inspect its registered tools without starting the stdio loop.
    """
    mcp = FastMCP("qlik-lineage-mcp")
    register_all(mcp)
    return mcp


def main() -> None:
    """Stdio entry point used by both ``python -m qlik_lineage_mcp.server``
    and the ``qlik-lineage-mcp`` console script defined in ``pyproject.toml``.

    Logs go to stderr because stdout is reserved for the MCP transport.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()


# Built: minimal ``server.py`` that delegates tool wiring to ``register_all``.
# Assumption: the ``mcp`` package exposes ``FastMCP`` at
# ``mcp.server.fastmcp.FastMCP`` (current as of mcp >= 1.2.0). If a future
# release renames it, this is the only file that needs to change.
