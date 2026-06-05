"""Parser tests against real Postman fixtures.

These tests pin the shapes we extract from the Qlik responses so a future
refactor (or a Qlik API change) breaks loudly instead of silently producing
wrong analyses.
"""

from __future__ import annotations

from qlik_lineage_mcp.models import FileFormat
from qlik_lineage_mcp.qlik_client import (
    parse_data_file,
    parse_lineage_graph,
)

from .conftest import (
    apps_from_fixture,
    data_files_from_fixture,
    app_fields_from_fixture,
    lineage_entries_from_fixture,
    lineage_graph_from_fixture,
    load_fixture,
    spaces_from_fixture,
)


# --- spaces -----------------------------------------------------------------

def test_parse_spaces_yields_id_name_type():
    spaces = spaces_from_fixture()
    assert len(spaces) > 0
    first = spaces[0]
    assert first.id  # non-empty UUID
    assert first.name  # non-empty display name
    assert first.type == "shared"


def test_space_type_values_observed_in_fixture():
    types = {s.type for s in spaces_from_fixture()}
    assert "shared" in types
    assert "managed" in types


# --- apps -------------------------------------------------------------------

def test_parse_apps_uses_resource_id_as_app_id():
    apps = apps_from_fixture("api_v1_items_space1.json")
    assert apps, "fixture should contain apps"
    sample = apps[0]
    assert sample.name  # non-empty display name
    assert sample.id    # non-empty UUID (resourceId, not item id)
    assert sample.app_file_size >= 0
    assert sample.space_id  # non-empty space UUID


# --- data files -------------------------------------------------------------

def test_parse_data_file_skips_dataasset_parent():
    """The space2 fixture has one ``dataasset`` (the DataFilesStore root) that
    must NOT be returned as a data file."""
    raw = load_fixture("api_v1_items_space2.json")
    parsed = [parse_data_file(item) for item in raw["data"]]
    nones = [p for p in parsed if p is None]
    assert nones, "expected at least one dataasset entry to be skipped"


def test_parse_data_file_extracts_qvd_metadata():
    files = data_files_from_fixture("api_v1_items_space2.json")
    assert any(f.format == FileFormat.QVD for f in files)
    sample = next(f for f in files if f.format == FileFormat.QVD)
    assert sample.format == FileFormat.QVD
    assert sample.qri and sample.qri.startswith("qdf:qix-datafiles:")
    assert sample.space_id  # non-empty space UUID


# --- app metadata -----------------------------------------------------------

def test_app_metadata_filters_system_and_hidden_fields():
    visible = app_fields_from_fixture("api_v1_apps_app1_data_metadata.json")
    names = {f.name for f in visible}
    # System fields like ``$Field``, ``$Table`` must be dropped.
    assert "$Field" not in names
    assert "$Table" not in names
    # Real columns should be present.
    assert "FILIAL" in names
    assert "VALOR" in names


# --- app lineage ------------------------------------------------------------

def test_app_lineage_entries_carry_discriminator_and_statement():
    entries = lineage_entries_from_fixture("ap1_v1_apps_ap1_data_lineage.json")
    assert entries
    libs = [e for e in entries if e.discriminator.startswith("lib://")]
    assert libs, "expected at least one lib:// source in fixture"
    residents = [e for e in entries if e.discriminator.startswith("RESIDENT")]
    assert residents, "expected at least one RESIDENT entry in fixture"


def test_app_lineage_store_pattern_visible():
    entries = lineage_entries_from_fixture("ap1_v1_apps_ap2_data_lineage.json")
    stores = [e for e in entries if e.discriminator.startswith("{STORE")]
    assert stores, "expected at least one STORE entry in fixture"


# --- lineage graphs ---------------------------------------------------------

def test_resource_lineage_graph_basic_shape():
    graph = lineage_graph_from_fixture("lineage_graphs_nodes_item3.json")
    assert graph.graph_type == "RESOURCE"
    assert graph.nodes
    assert graph.edges
    # The item3 fixture is the Fornecedores QVD: one app (PROCESSOR) +
    # one dataset (FILE) with a STORE edge between them.
    types = {n.type for n in graph.nodes}
    assert types == {"DA_APP", "DATASET"}
    relations = {e.relation for e in graph.edges}
    assert "STORE" in relations


def test_field_lineage_graph_yields_field_nodes():
    graph = lineage_graph_from_fixture("lineage_graphs_nodes_level_field_item3.json")
    assert graph.graph_type == "FIELD"
    assert all(n.type == "FIELD" for n in graph.nodes)
    # All field labels should be non-empty.
    assert all(n.label for n in graph.nodes)


# --- size enrichment from /api/v1/data-files --------------------------------

import respx
import httpx
from qlik_lineage_mcp.config import Settings
from qlik_lineage_mcp.qlik_client import QlikClient


@respx.mock
async def test_list_data_files_enriches_size_from_data_files_endpoint():
    """Sizes from /api/v1/data-files are merged into DataFile.estimated_size_bytes."""
    base = "https://t.qlikcloud.com"
    settings = Settings(tenant_url=base, api_key="test-key", request_timeout_s=10)

    respx.get(f"{base}/api/v1/items").mock(return_value=httpx.Response(200, json={
        "data": [{
            "resourceType": "dataset",
            "name": "Sales.qvd",
            "spaceId": "sp1",
            "resourceAttributes": {"type": "qvd", "qri": "qdf:qix-datafiles:abc"},
            "resourceSize": {"appFile": 0},
        }],
        "links": {},
    }))
    respx.get(f"{base}/api/v1/data-files").mock(return_value=httpx.Response(200, json={
        "data": [{"name": "Sales.qvd", "size": 52428800}],
        "links": {},
    }))

    async with QlikClient(settings) as client:
        files = await client.list_data_files_in_space("sp1")

    assert len(files) == 1
    assert files[0].name == "Sales.qvd"
    assert files[0].estimated_size_bytes == 52428800


@respx.mock
async def test_list_data_files_size_zero_when_not_in_data_files():
    """Files absent from /data-files keep estimated_size_bytes == 0."""
    base = "https://t.qlikcloud.com"
    settings = Settings(tenant_url=base, api_key="test-key", request_timeout_s=10)

    respx.get(f"{base}/api/v1/items").mock(return_value=httpx.Response(200, json={
        "data": [{
            "resourceType": "dataset",
            "name": "Orphan.qvd",
            "spaceId": "sp1",
            "resourceAttributes": {"type": "qvd", "qri": "qdf:qix-datafiles:xyz"},
            "resourceSize": {"appFile": 0},
        }],
        "links": {},
    }))
    respx.get(f"{base}/api/v1/data-files").mock(return_value=httpx.Response(200, json={
        "data": [],
        "links": {},
    }))

    async with QlikClient(settings) as client:
        files = await client.list_data_files_in_space("sp1")

    assert files[0].estimated_size_bytes == 0
