"""Shared pytest helpers.

The tests deliberately avoid any live HTTP traffic — every Qlik response
is replayed from ``tests/fixtures/``. A ``FakeQlikClient`` here implements
the same async interface as ``QlikClient`` and is wired with whatever
fixture data each test needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from qlik_lineage_mcp.session_cache import _cache
from qlik_lineage_mcp.models import (
    App,
    AppField,
    DataFile,
    LineageEntry,
    LineageGraph,
    Space,
)
from qlik_lineage_mcp.qlik_client import (
    parse_app,
    parse_app_field,
    parse_data_file,
    parse_lineage_graph,
    parse_space,
)


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict | list:
    """Read a JSON fixture by filename relative to ``tests/fixtures/``."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def clear_session_cache():
    """Isolate each test from shared cache state."""
    _cache.clear()
    yield
    _cache.clear()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@dataclass
class FakeQlikClient:
    """Minimal in-memory stand-in for ``QlikClient``.

    Tools call a small interface (list_spaces, list_apps_in_tenant, ...)
    and we only need to implement that. Tests populate the attributes
    below before running the tool function.
    """

    spaces: list[Space] = field(default_factory=list)
    apps_by_space: dict[str, list[App]] = field(default_factory=dict)
    apps_in_tenant: list[App] = field(default_factory=list)
    data_files_by_space: dict[str, list[DataFile]] = field(default_factory=dict)
    app_fields: dict[str, list[AppField]] = field(default_factory=dict)
    # Apps whose metadata fetch should raise (set membership matters; values
    # ignored). Used to exercise the metadata_unavailable_apps code path.
    app_metadata_errors: set[str] = field(default_factory=set)
    app_lineage: dict[str, list[LineageEntry]] = field(default_factory=dict)
    lineage_graphs: dict[tuple[str, str], LineageGraph] = field(default_factory=dict)

    # ---- async surface mirrors QlikClient ----
    async def list_spaces(self) -> list[Space]:
        return list(self.spaces)

    async def find_space_by_name(self, name: str) -> Optional[Space]:
        target = name.strip().lower()
        for s in self.spaces:
            if s.name.lower() == target:
                return s
        return None

    async def list_apps_in_space(self, space_id: str) -> list[App]:
        return list(self.apps_by_space.get(space_id, []))

    async def list_apps_in_tenant(self) -> list[App]:
        return list(self.apps_in_tenant)

    async def list_data_files_in_space(self, space_id: str) -> list[DataFile]:
        return list(self.data_files_by_space.get(space_id, []))

    async def get_app_fields(
        self, app_id: str, include_system: bool = False
    ) -> list[AppField]:
        if app_id in self.app_metadata_errors:
            raise RuntimeError(f"simulated metadata error for {app_id}")
        fields = self.app_fields.get(app_id, [])
        if include_system:
            return list(fields)
        return [f for f in fields if not (f.is_system or f.is_hidden)]

    async def get_app_lineage(self, app_id: str) -> list[LineageEntry]:
        return list(self.app_lineage.get(app_id, []))

    async def get_lineage_graph(
        self, qri: str, level: str = "resource"
    ) -> LineageGraph:
        try:
            return self.lineage_graphs[(qri, level)]
        except KeyError as e:
            raise RuntimeError(
                f"No fixture lineage graph for ({qri!r}, level={level!r})"
            ) from e


@pytest.fixture
def fake_client() -> FakeQlikClient:
    return FakeQlikClient()


# -- helpers used by multiple test modules -----------------------------------

def spaces_from_fixture() -> list[Space]:
    raw = load_fixture("api_v1_spaces.json")
    return [parse_space(item) for item in raw["data"]]


def apps_from_fixture(filename: str) -> list[App]:
    raw = load_fixture(filename)
    return [parse_app(item) for item in raw["data"] if item.get("resourceType") == "app"]


def data_files_from_fixture(filename: str) -> list[DataFile]:
    raw = load_fixture(filename)
    out: list[DataFile] = []
    for item in raw["data"]:
        df = parse_data_file(item)
        if df is not None:
            out.append(df)
    return out


def app_fields_from_fixture(filename: str, include_system: bool = False) -> list[AppField]:
    raw = load_fixture(filename)
    out = [parse_app_field(f) for f in raw.get("fields", [])]
    if include_system:
        return out
    return [f for f in out if not (f.is_system or f.is_hidden)]


def lineage_graph_from_fixture(filename: str) -> LineageGraph:
    return parse_lineage_graph(load_fixture(filename))


def lineage_entries_from_fixture(filename: str) -> list[LineageEntry]:
    raw = load_fixture(filename)
    return [LineageEntry.model_validate(x) for x in raw]
