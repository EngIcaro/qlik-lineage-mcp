"""Runtime configuration loader.

All secrets and tenant-specific values live in environment variables (or a
local ``.env`` file). Nothing in this module ever logs or returns the API
key — only ``Settings.api_key`` exposes it.

We intentionally avoid a ``python-dotenv`` dependency: ``_load_dotenv`` here
is small enough that pulling in a third-party package is not worth it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# Walks from CWD upward looking for a .env. The first one found wins.
# Real OS env vars take precedence over .env values, so production deployments
# can override secrets without touching the file.
def _load_dotenv() -> None:
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        env_file = parent / ".env"
        if not env_file.exists():
            continue
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return


@dataclass(frozen=True)
class Settings:
    """Immutable runtime config. Build once at startup; pass around explicitly."""

    tenant_url: str          # e.g. "https://my-tenant.us.qlikcloud.com"
    api_key: str             # bearer token
    request_timeout_s: float  # per-request HTTP timeout

    def safe_repr(self) -> str:
        """Helper used by logs — never include the API key verbatim."""
        return f"Settings(tenant_url={self.tenant_url!r}, request_timeout_s={self.request_timeout_s})"


def load_settings() -> Settings:
    """Read configuration from the environment.

    Raises ``RuntimeError`` if a required variable is missing, so the server
    fails fast at startup instead of producing confusing 401s later.
    """
    _load_dotenv()
    tenant_url = os.environ.get("QLIK_TENANT_URL", "").strip()
    api_key = os.environ.get("QLIK_API_KEY", "").strip()
    if not tenant_url:
        raise RuntimeError("QLIK_TENANT_URL is not set (see .env.example).")
    if not api_key:
        raise RuntimeError("QLIK_API_KEY is not set (see .env.example).")
    return Settings(
        tenant_url=tenant_url.rstrip("/"),
        api_key=api_key,
        request_timeout_s=float(os.environ.get("QLIK_REQUEST_TIMEOUT", "30")),
    )


# Built: ``config.py`` parses ``.env`` and exposes ``Settings``.
# Assumption: tenant URL has the form ``https://<tenant>.<region>.qlikcloud.com``
# (e.g. ``https://your-tenant.us.qlikcloud.com``).
# No Parquet-specific TODO here — config is format-agnostic.
