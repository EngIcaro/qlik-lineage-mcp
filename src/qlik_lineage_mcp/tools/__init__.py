"""Tool auto-registration.

Every submodule that exports a top-level ``register(mcp: FastMCP) -> None``
function is automatically discovered and called by ``register_all``. This
removes the central edit point that turns ``server.py`` into a junk drawer
as the project grows: **adding a tool is one file**, with no edits anywhere
else.

Conventions for tool modules:
- File name starts with a letter (modules starting with ``_`` are skipped).
- Module exposes a function ``register(mcp)`` that decorates one or more
  callables with ``@mcp.tool()``.
- The module is responsible for instantiating its own dependencies
  (typically a ``QlikClient``) at call time, not at import time, so an
  import never triggers network I/O.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_all(mcp: "FastMCP") -> None:
    """Discover every tool module in this package and call its ``register``."""
    for mod_info in pkgutil.iter_modules(__path__):
        # Skip private / dunder helpers (``__init__`` is not listed by
        # ``iter_modules`` but defensive in case a ``_helpers.py`` is added
        # later).
        if mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{mod_info.name}")
        register = getattr(module, "register", None)
        if register is None:
            logger.warning(
                "Tool module %s has no register(mcp) — skipped.",
                mod_info.name,
            )
            continue
        register(mcp)
        logger.info("Registered tool module: %s", mod_info.name)
