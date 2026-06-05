"""Module-level TTL cache shared across QlikClient instances within a process.

MCP tool handlers create a fresh QlikClient per invocation, so this cache
lives outside the client to persist across calls within the same Claude
session. The cache is namespaced by tenant URL so a process serving
multiple tenants does not cross-contaminate results.

TTL defaults to 300 s (5 min) — enough to cover back-to-back calls to
ghost_files and unused_columns without the underlying Qlik data changing
meaningfully between them.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_TTL_S: int = 300  # 5 minutes


class SessionCache:
    """In-memory TTL cache keyed by arbitrary tuples."""

    def __init__(self, ttl_s: int = _TTL_S) -> None:
        self._ttl = ttl_s
        self._store: dict[tuple, tuple[float, Any]] = {}

    def get(self, key: tuple) -> tuple[bool, Any]:
        """Return ``(hit, value)``. Expired entries are evicted on read."""
        entry = self._store.get(key)
        if entry is None:
            return False, None
        ts, value = entry
        if time.monotonic() - ts > self._ttl:
            del self._store[key]
            logger.debug("cache EXPIRED  %s", key)
            return False, None
        logger.debug("cache HIT      %s", key)
        return True, value

    def set(self, key: tuple, value: Any) -> None:
        logger.debug("cache SET      %s", key)
        self._store[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._store.clear()


# Module-level singleton — one cache per process, shared across all
# QlikClient instances and MCP tool invocations.
_cache = SessionCache()
