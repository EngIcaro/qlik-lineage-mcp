"""Tests for the ``ghost_files`` tool.

Covers the discriminator classifier, the fixpoint that detects chain
ghosts, and the end-to-end analysis against synthetic apps/files.
"""

from __future__ import annotations

import pytest

from qlik_lineage_mcp.models import (
    App,
    DataFile,
    FileFormat,
    LineageEntry,
    Space,
)
from qlik_lineage_mcp.tools.ghost_files import (
    analyze_ghost_files,
    classify_discriminator,
    compute_useful_files,
)

from .conftest import FakeQlikClient


# ---------------------------------------------------------------------------
# classify_discriminator
# ---------------------------------------------------------------------------

class TestClassifyDiscriminator:
    def test_store_pattern(self):
        kind, name = classify_discriminator(
            "{STORE - [lib://CONN:DataFiles/Out.qvd](qvd)};"
        )
        assert kind == "store"
        assert name == "out.qvd"

    def test_load_pattern(self):
        kind, name = classify_discriminator(
            "lib://CONN:DataFiles/bronze_example.qvd;"
        )
        assert kind == "load"
        assert name == "bronze_example.qvd"

    def test_resident_other(self):
        assert classify_discriminator("RESIDENT MY_TABLE;") == ("other", "")

    def test_db_connection_other(self):
        assert classify_discriminator("{AF_Connections:db_name};") == ("other", "")

    def test_autogenerate_other(self):
        assert classify_discriminator("AUTOGENERATE;") == ("other", "")

    def test_blank_other(self):
        assert classify_discriminator("") == ("other", "")
        assert classify_discriminator("   ") == ("other", "")


# ---------------------------------------------------------------------------
# Fixpoint: compute_useful_files
# ---------------------------------------------------------------------------

class TestComputeUsefulFiles:
    def test_leaf_consumer_marks_files_useful(self):
        apps = [App(id="A1", name="Final Dashboard")]
        consumes = {"A1": {"sales.qvd"}}
        produces: dict = {}
        useful = compute_useful_files(apps, consumes, produces)
        assert useful == {"sales.qvd"}

    def test_chain_via_prep_app(self):
        # B1 loads bronze.qvd and stores silver.qvd; D1 loads silver.qvd
        # and produces nothing -> bronze.qvd is useful through the chain.
        apps = [
            App(id="B1", name="Bronze Prep"),
            App(id="D1", name="Dashboard"),
        ]
        consumes = {"B1": {"bronze.qvd"}, "D1": {"silver.qvd"}}
        produces = {"B1": {"silver.qvd"}}
        useful = compute_useful_files(apps, consumes, produces)
        assert useful == {"bronze.qvd", "silver.qvd"}

    def test_dead_end_chain_is_not_useful(self):
        # B1 loads bronze.qvd and stores silver.qvd, but nothing consumes
        # silver.qvd -> both bronze.qvd and silver.qvd are ghosts.
        apps = [App(id="B1", name="Bronze Prep")]
        consumes = {"B1": {"bronze.qvd"}}
        produces = {"B1": {"silver.qvd"}}
        useful = compute_useful_files(apps, consumes, produces)
        assert useful == set()

    def test_consumer_with_no_load_is_not_useful(self):
        # An app with no consumed files cannot keep anything alive.
        apps = [App(id="A1", name="Idle")]
        useful = compute_useful_files(apps, consumes={}, produces={})
        assert useful == set()


# ---------------------------------------------------------------------------
# End-to-end tool logic
# ---------------------------------------------------------------------------

async def test_analyze_ghost_files_finds_unused_file(fake_client: FakeQlikClient):
    space = Space(id="SP1", name="Silver", type="shared")
    f_used = DataFile(name="UsedByApp.qvd", space_id="SP1", format=FileFormat.QVD,
                      estimated_size_bytes=1024)
    f_ghost = DataFile(name="NobodyReadsThis.qvd", space_id="SP1",
                       format=FileFormat.QVD, estimated_size_bytes=2 * 1024 ** 3)
    fake_client.spaces = [space]
    fake_client.data_files_by_space = {"SP1": [f_used, f_ghost]}

    app = App(id="A1", name="Dashboard")
    fake_client.apps_in_tenant = [app]
    fake_client.app_lineage = {
        "A1": [
            LineageEntry(
                discriminator="lib://SilverConn:DataFiles/UsedByApp.qvd;",
                statement="",
            ),
        ],
    }

    result = await analyze_ghost_files(fake_client, "Silver")
    assert "error" not in result
    names = [g["name"] for g in result["ghost_files"]]
    assert names == ["NobodyReadsThis.qvd"]
    assert result["summary"]["ghost_count"] == 1
    assert result["summary"]["total_files_in_space"] == 2
    # Estimated GB gain comes through (~2 GiB).
    assert result["summary"]["estimated_total_gb_gain"] >= 1.9


async def test_analyze_ghost_files_detects_chain_ghost(fake_client: FakeQlikClient):
    """A QVD only consumed by a prep app whose own output is a ghost
    must itself be reported as a ghost."""
    space = Space(id="SP1", name="Silver", type="shared")
    bronze = DataFile(name="Bronze.qvd", space_id="SP1", format=FileFormat.QVD)
    silver = DataFile(name="Silver.qvd", space_id="SP1", format=FileFormat.QVD)
    fake_client.spaces = [space]
    fake_client.data_files_by_space = {"SP1": [bronze, silver]}

    prep = App(id="P1", name="Bronze to Silver")
    fake_client.apps_in_tenant = [prep]
    fake_client.app_lineage = {
        "P1": [
            LineageEntry(
                discriminator="lib://Conn:DataFiles/Bronze.qvd;",
                statement="",
            ),
            LineageEntry(
                discriminator="{STORE - [lib://Conn:DataFiles/Silver.qvd](qvd)};",
                statement="",
            ),
        ],
    }
    result = await analyze_ghost_files(fake_client, "Silver")
    names = sorted(g["name"] for g in result["ghost_files"])
    assert names == ["Bronze.qvd", "Silver.qvd"]


async def test_analyze_ghost_files_keeps_useful_chain(fake_client: FakeQlikClient):
    """When a prep app's output IS consumed by a final app, both the prep
    input AND the prep output are useful (no ghosts)."""
    space = Space(id="SP1", name="Silver", type="shared")
    bronze = DataFile(name="Bronze.qvd", space_id="SP1", format=FileFormat.QVD)
    silver = DataFile(name="Silver.qvd", space_id="SP1", format=FileFormat.QVD)
    fake_client.spaces = [space]
    fake_client.data_files_by_space = {"SP1": [bronze, silver]}

    prep = App(id="P1", name="Bronze to Silver")
    dash = App(id="D1", name="Dashboard")
    fake_client.apps_in_tenant = [prep, dash]
    fake_client.app_lineage = {
        "P1": [
            LineageEntry(discriminator="lib://Conn:DataFiles/Bronze.qvd;"),
            LineageEntry(discriminator="{STORE - [lib://Conn:DataFiles/Silver.qvd](qvd)};"),
        ],
        "D1": [
            LineageEntry(discriminator="lib://Conn:DataFiles/Silver.qvd;"),
        ],
    }
    result = await analyze_ghost_files(fake_client, "Silver")
    assert result["ghost_files"] == []
    assert result["summary"]["ghost_count"] == 0


async def test_analyze_ghost_files_unknown_space(fake_client: FakeQlikClient):
    fake_client.spaces = []
    result = await analyze_ghost_files(fake_client, "Nope")
    assert "error" in result
    assert "Nope" in result["error"]
