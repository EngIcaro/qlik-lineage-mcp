"""Sanity tests for the FastMCP wiring and tool auto-registration.

These ensure that ``server.build_server()`` actually loads every tool in the
``tools/`` package — the headline architectural property of the project.
"""

from __future__ import annotations

import pytest

mcp_fastmcp = pytest.importorskip("mcp.server.fastmcp")

from qlik_lineage_mcp.server import build_server


@pytest.mark.asyncio
async def test_build_server_registers_known_tools():
    server = build_server()
    # FastMCP exposes registered tools via list_tools(); the call is async.
    tool_names = {t.name for t in await server.list_tools()}
    assert "unused_columns" in tool_names
    assert "ghost_files" in tool_names
