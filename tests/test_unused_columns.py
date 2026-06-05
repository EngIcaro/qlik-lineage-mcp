"""Tests for the ``unused_columns`` tool (3-phase pipeline).

Covers the pure helpers, the end-to-end algorithm on synthetic graphs,
and one integration test against a captured field-level lineage fixture
(requires local fixture files — not committed to the repository).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from qlik_lineage_mcp.models import (
    App,
    AppField,
    DataFile,
    FileFormat,
    LineageEdge,
    LineageEntry,
    LineageGraph,
    LineageNode,
    Space,
)
from qlik_lineage_mcp.tools.unused_columns import (
    _columns_from_field_graph,
    _extract_renames_from_consumer_graph,
    _parse_activation_date,
    analyze_unused_columns,
)

from .conftest import FakeQlikClient, lineage_graph_from_fixture


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestColumnsFromFieldGraph:
    def test_only_picks_file_side_nodes(self):
        file_qri = "qri:qdf:space://SP#FILE"
        graph = LineageGraph(
            graph_type="FIELD",
            nodes=[
                LineageNode(qri=f"{file_qri}#t#1", label="ColA", type="FIELD"),
                LineageNode(qri=f"{file_qri}#t#2", label="ColB", type="FIELD"),
                LineageNode(
                    qri="qri:app:dataprep://APP#t#9",
                    label="ColA",  # same label, but app-side -> must be ignored
                    type="FIELD",
                ),
            ],
            edges=[],
        )
        assert _columns_from_field_graph(graph, file_qri) == ["ColA", "ColB"]


class TestExtractRenamesFromConsumerGraph:
    def test_picks_only_edges_sourced_at_file_field(self):
        """Only edges whose source is a field of the target file count;
        edges among other nodes in the consumer's lineage must be ignored."""
        file_qri = "qri:qdf:space://SP#FILE"
        app_qri = "qri:app:sense://APP1"
        graph = LineageGraph(
            graph_type="FIELD",
            nodes=[
                LineageNode(qri=f"{file_qri}#t#1", label="OrigA", type="FIELD"),
                LineageNode(qri=f"{file_qri}#t#2", label="OrigB", type="FIELD"),
                LineageNode(qri=f"{app_qri}#x#1", label="alias_a", type="FIELD"),
                LineageNode(qri=f"{app_qri}#x#2", label="alias_b", type="FIELD"),
                # Unrelated dataprep field in the consumer's upstream
                LineageNode(qri="qri:app:dataprep://OTHER#y#1", label="ignored", type="FIELD"),
            ],
            edges=[
                LineageEdge(relation="from", source=f"{file_qri}#t#1", target=f"{app_qri}#x#1"),
                LineageEdge(relation="from", source=f"{file_qri}#t#2", target=f"{app_qri}#x#2"),
                # Edge that does NOT originate at our file -> ignored
                LineageEdge(
                    relation="from",
                    source="qri:app:dataprep://OTHER#y#1",
                    target=f"{app_qri}#x#1",
                ),
            ],
        )
        renames = _extract_renames_from_consumer_graph(graph, file_qri)
        labels = {(o, a) for (o, a, _qri, _rel) in renames}
        assert labels == {("OrigA", "alias_a"), ("OrigB", "alias_b")}

    def test_composite_expression_emits_one_edge_per_input(self):
        """Mirrors the real Qlik behavior:
        ``A1_COD & '\\' & A1_LOJA AS KEY_CLIENTE`` -> two edges sharing
        the same target alias."""
        file_qri = "qri:qdf:space://SP#FILE"
        app_qri = "qri:app:sense://APP1"
        graph = LineageGraph(
            graph_type="FIELD",
            nodes=[
                LineageNode(qri=f"{file_qri}#t#a", label="A1_COD", type="FIELD"),
                LineageNode(qri=f"{file_qri}#t#b", label="A1_LOJA", type="FIELD"),
                LineageNode(qri=f"{app_qri}#x#k", label="KEY_CLIENTE", type="FIELD"),
            ],
            edges=[
                LineageEdge(relation="from", source=f"{file_qri}#t#a", target=f"{app_qri}#x#k"),
                LineageEdge(relation="from", source=f"{file_qri}#t#b", target=f"{app_qri}#x#k"),
            ],
        )
        renames = _extract_renames_from_consumer_graph(graph, file_qri)
        aliases_by_col = {o: a for (o, a, _q, _r) in renames}
        assert aliases_by_col == {"A1_COD": "KEY_CLIENTE", "A1_LOJA": "KEY_CLIENTE"}


class TestAppLineageQri:
    def test_sense_app_default(self):
        assert (
            App(id="UUID", name="x", usage="ANALYTICS").lineage_qri
            == "qri:app:sense://UUID"
        )

    def test_dataprep_app(self):
        assert (
            App(id="UUID", name="x", usage="DATA_PREPARATION").lineage_qri
            == "qri:app:dataprep://UUID"
        )

    def test_unknown_usage_defaults_to_sense(self):
        assert App(id="UUID", name="x").lineage_qri == "qri:app:sense://UUID"


# ---------------------------------------------------------------------------
# End-to-end on synthetic data
# ---------------------------------------------------------------------------

FILE_QRI = "qri:qdf:space://SP1#FILE"
SPACE = Space(id="SP1", name="Finance", type="shared")


def _setup_file(
    fake_client: FakeQlikClient,
    columns: list[str],
    file_format: FileFormat = FileFormat.QVD,
) -> DataFile:
    target = DataFile(name="Customers.qvd", space_id="SP1", qri=FILE_QRI, format=file_format)
    nodes = [
        LineageNode(qri=f"{FILE_QRI}#t#{i}", label=c, type="FIELD")
        for i, c in enumerate(columns)
    ]
    file_graph = LineageGraph(graph_type="FIELD", nodes=nodes, edges=[])
    fake_client.spaces = [SPACE]
    fake_client.data_files_by_space = {"SP1": [target]}
    fake_client.lineage_graphs = {(FILE_QRI, "field"): file_graph}
    return target


async def test_direct_match_only_counts_consumer_apps(fake_client: FakeQlikClient):
    """An app that has matching field names but does NOT consume the file
    must NOT count as evidence of usage. This is the fix for the false-
    positive bug seen against the real tenant (SE1010 marked 299/299 used
    because the union of all 1880 tenant apps had every name)."""
    target = _setup_file(fake_client, ["CustomerID", "Email", "Phone"])
    # App "Lurker" has CustomerID and Email in its metadata but does NOT
    # load Customers.qvd. Its metadata must not contaminate the verdict.
    lurker = App(id="LURK", name="Unrelated Dashboard", usage="ANALYTICS")
    # App "Real" actually loads Customers.qvd and has CustomerID + Email.
    real = App(id="REAL", name="Real Consumer", usage="ANALYTICS")
    fake_client.apps_in_tenant = [lurker, real]
    fake_client.app_fields = {
        "LURK": [AppField(name="CustomerID"), AppField(name="Email"), AppField(name="Phone")],
        "REAL": [AppField(name="CustomerID"), AppField(name="Email")],
    }
    fake_client.app_lineage = {
        "LURK": [],  # not a consumer
        "REAL": [LineageEntry(discriminator=f"lib://x:DataFiles/{target.name};")],
    }

    result = await analyze_unused_columns(fake_client, target.name, "Finance")
    assert "error" not in result
    # Only REAL counts. CustomerID and Email are in REAL's metadata.
    # Phone is NOT in REAL's metadata (only in LURK's, which is ignored).
    assert result["summary"]["consumer_apps_found"] == 1
    assert result["summary"]["used_direct_count"] == 2
    assert result["unused_columns"] == ["Phone"]
    used_cols = {u["column"] for u in result["used_direct"]}
    assert used_cols == {"CustomerID", "Email"}
    # Each used-direct entry must cite REAL (not LURK).
    for entry in result["used_direct"]:
        for ev in entry["used_in"]:
            assert ev["app_id"] == "REAL"


async def test_rename_via_consumer_lineage(fake_client: FakeQlikClient):
    """The consumer's field-level lineage exposes a rename edge:
    ``Email -> customer_email``. The column is reported as used-with-rename."""
    target = _setup_file(fake_client, ["CustomerID", "Email", "Phone"])
    app = App(id="A1", name="Sales Dashboard", usage="ANALYTICS")
    fake_client.apps_in_tenant = [app]
    fake_client.app_fields = {"A1": [AppField(name="customer_email")]}
    fake_client.app_lineage = {
        "A1": [
            LineageEntry(
                discriminator=f"lib://X:DataFiles/{target.name};",
                statement="",
            )
        ],
    }
    # Field-level lineage of A1 — one rename edge from Email -> customer_email.
    app_qri = "qri:app:sense://A1"
    consumer_graph = LineageGraph(
        graph_type="FIELD",
        nodes=[
            LineageNode(qri=f"{FILE_QRI}#t#1", label="Email", type="FIELD"),
            LineageNode(qri=f"{app_qri}#x#1", label="customer_email", type="FIELD"),
        ],
        edges=[
            LineageEdge(relation="from", source=f"{FILE_QRI}#t#1", target=f"{app_qri}#x#1"),
        ],
    )
    fake_client.lineage_graphs[(app_qri, "field")] = consumer_graph

    result = await analyze_unused_columns(fake_client, target.name, "Finance")
    assert result["summary"]["consumer_apps_found"] == 1
    assert result["used_with_rename"] == [
        {
            "column": "Email",
            "renamed_in": [
                {
                    "app_id": "A1",
                    "app_name": "Sales Dashboard",
                    "alias": "customer_email",
                    "relation": "from",
                }
            ],
        }
    ]
    # Phone and CustomerID have neither direct match nor rename evidence.
    assert sorted(result["unused_columns"]) == ["CustomerID", "Phone"]


async def test_composite_expression_marks_both_inputs_used(fake_client: FakeQlikClient):
    """The classic E-SHOP case: ``A1_COD & '\\' & A1_LOJA AS KEY_CLIENTE``
    must mark both A1_COD and A1_LOJA as used-with-rename, sharing the
    same alias label."""
    target = _setup_file(fake_client, ["A1_COD", "A1_LOJA", "A1_TIPO"])
    app = App(id="ESHOP", name="E-SHOP Sales", usage="ANALYTICS")
    fake_client.apps_in_tenant = [app]
    fake_client.app_fields = {"ESHOP": [AppField(name="KEY_CLIENTE")]}
    fake_client.app_lineage = {
        "ESHOP": [LineageEntry(discriminator=f"lib://X:DataFiles/{target.name};")],
    }
    app_qri = "qri:app:sense://ESHOP"
    consumer_graph = LineageGraph(
        graph_type="FIELD",
        nodes=[
            LineageNode(qri=f"{FILE_QRI}#t#a", label="A1_COD", type="FIELD"),
            LineageNode(qri=f"{FILE_QRI}#t#b", label="A1_LOJA", type="FIELD"),
            LineageNode(qri=f"{app_qri}#x#k", label="KEY_CLIENTE", type="FIELD"),
        ],
        edges=[
            LineageEdge(relation="from", source=f"{FILE_QRI}#t#a", target=f"{app_qri}#x#k"),
            LineageEdge(relation="from", source=f"{FILE_QRI}#t#b", target=f"{app_qri}#x#k"),
        ],
    )
    fake_client.lineage_graphs[(app_qri, "field")] = consumer_graph

    result = await analyze_unused_columns(fake_client, target.name, "Finance")
    used_cols = {u["column"] for u in result["used_with_rename"]}
    assert used_cols == {"A1_COD", "A1_LOJA"}
    assert result["unused_columns"] == ["A1_TIPO"]


async def test_consumer_lineage_failure_lists_app(fake_client: FakeQlikClient):
    """When a consumer's field-level lineage cannot be fetched, the app is
    listed in ``consumer_lineage_failures`` and the recommendation reflects
    that the verdict is conditional."""
    target = _setup_file(fake_client, ["A", "B"])
    app = App(id="A1", name="Broken Lineage")
    fake_client.apps_in_tenant = [app]
    fake_client.app_fields = {"A1": []}
    fake_client.app_lineage = {
        "A1": [LineageEntry(discriminator=f"lib://X:DataFiles/{target.name};")],
    }
    # Note: no lineage graph registered for the consumer -> FakeClient raises.

    result = await analyze_unused_columns(fake_client, target.name, "Finance")
    assert result["summary"]["consumer_apps_found"] == 1
    assert result["summary"]["consumer_lineage_extracted"] == 0
    failures = result["consumer_lineage_failures"]
    assert len(failures) == 1
    assert failures[0]["app_id"] == "A1"
    assert "A1" in result["recommendation"]["conditional_on_apps_we_could_not_inspect"]
    assert any("consumer_lineage_failures" in d for d in result["disclaimers"])


async def test_metadata_failure_listed_globally(fake_client: FakeQlikClient):
    """Both apps are consumers; one has metadata working, the other errors.
    The verdict relies on the healthy consumer's metadata and the broken
    one is surfaced as a caveat."""
    target = _setup_file(fake_client, ["A", "B"])
    healthy = App(id="OK", name="Healthy")
    broken = App(id="BAD", name="Broken")
    fake_client.apps_in_tenant = [healthy, broken]
    fake_client.app_fields = {"OK": [AppField(name="A")]}
    fake_client.app_metadata_errors = {"BAD"}
    consumer_entry = [LineageEntry(discriminator=f"lib://x:DataFiles/{target.name};")]
    fake_client.app_lineage = {"OK": consumer_entry, "BAD": consumer_entry}

    result = await analyze_unused_columns(fake_client, target.name, "Finance")
    assert result["unused_columns"] == ["B"]
    assert result["metadata_unavailable_apps"] == [{"app_id": "BAD", "app_name": "Broken"}]
    assert "BAD" in result["recommendation"]["conditional_on_apps_we_could_not_inspect"]


async def test_missing_space_returns_error(fake_client: FakeQlikClient):
    fake_client.spaces = []
    result = await analyze_unused_columns(fake_client, "X.qvd", "Nope")
    assert "error" in result and "Nope" in result["error"]


async def test_missing_file_returns_error(fake_client: FakeQlikClient):
    fake_client.spaces = [SPACE]
    fake_client.data_files_by_space = {"SP1": []}
    result = await analyze_unused_columns(fake_client, "Missing.qvd", "Finance")
    assert "error" in result and "Missing.qvd" in result["error"]


async def test_parquet_disclaimer(fake_client: FakeQlikClient):
    target = _setup_file(fake_client, ["Col1"], file_format=FileFormat.PARQUET)
    fake_client.apps_in_tenant = []
    result = await analyze_unused_columns(fake_client, target.name, "Finance")
    assert any("Parquet" in d for d in result["disclaimers"])


# ---------------------------------------------------------------------------
# Integration test against the captured E-SHOP Sales fixture.
# ---------------------------------------------------------------------------

async def test_integration_fixture_field_lineage(fake_client: FakeQlikClient):
    """Integration test against a captured field-level lineage fixture.

    A consumer app loads the target QVD with:

        LOAD A1_COD & '\\' & A1_LOJA AS KEY_CLIENTE,
             A1_PESSOA AS TIPO_PESSOA
        FROM target.qvd;

    So the tool should:
      - Enumerate 20 SA1010 columns from the file-side fixture
      - Find E-SHOP Sales as a consumer
      - Detect renames: A1_COD->KEY_CLIENTE, A1_LOJA->KEY_CLIENTE,
        A1_PESSOA->TIPO_PESSOA
      - Report the remaining 17 SA1010 columns as unused
    """
    sa1010_qri = (
        "qri:qdf:space://mug-EYYZIWJBMKVUUxMV7tEFMFhd3DBD-PZFFlpMw_I"
        "#RlMF09tD99ToDZ2KypwZHLHdWsB06kPsGYU4fCFc2SU"
    )
    space = Space(id="SP_BRONZE", name="Bronze", type="shared")
    target = DataFile(
        name="target.qvd",
        space_id="SP_BRONZE",
        qri=sa1010_qri,
        format=FileFormat.QVD,
    )
    fake_client.spaces = [space]
    fake_client.data_files_by_space = {"SP_BRONZE": [target]}
    fake_client.lineage_graphs = {
        # File-side lineage (item4 captured from /nodes/{file}?level=field).
        (sa1010_qri, "field"): lineage_graph_from_fixture(
            "lineage_graphs_nodes_level_field_item4.json"
        ),
    }

    # The E-SHOP Sales consumer app (Sense / ANALYTICS).
    eshop = App(
        id="44929290-843a-48ec-888e-5a388c738040",
        name="E-SHOP Sales",
        usage="ANALYTICS",
    )
    fake_client.apps_in_tenant = [eshop]
    # E-SHOP's data/lineage must reference SA1010 for it to be classified
    # as a consumer.
    fake_client.app_lineage = {
        eshop.id: [
            LineageEntry(
                discriminator="lib://CONN:DataFiles/target.qvd;"
            )
        ],
    }
    # E-SHOP's metadata: KEY_CLIENTE and TIPO_PESSOA must be present so
    # that the aliases are recognized as actually loaded fields. (We only
    # need these two — the test fixture has many more, but for the unused-
    # columns logic only the alias presence matters.)
    fake_client.app_fields = {
        eshop.id: [AppField(name="KEY_CLIENTE"), AppField(name="TIPO_PESSOA")],
    }
    # E-SHOP's field-level lineage (the captured fixture).
    fake_client.lineage_graphs[(eshop.lineage_qri, "field")] = (
        lineage_graph_from_fixture("lineage_nodes_field_eshop_sales.json")
    )

    result = await analyze_unused_columns(fake_client, target.name, "Bronze")
    assert "error" not in result
    assert result["summary"]["total_columns"] == 20
    assert result["summary"]["consumer_apps_found"] == 1
    assert result["summary"]["consumer_lineage_extracted"] == 1

    # The renamed columns we expect to detect.
    renamed_cols = {u["column"] for u in result["used_with_rename"]}
    assert renamed_cols == {"A1_COD", "A1_LOJA", "A1_PESSOA"}

    # A1_COD and A1_LOJA should each carry KEY_CLIENTE as alias evidence
    # (composite expression decomposed by Qlik).
    aliases_for_cod = {
        ev["alias"]
        for u in result["used_with_rename"]
        if u["column"] == "A1_COD"
        for ev in u["renamed_in"]
    }
    aliases_for_loja = {
        ev["alias"]
        for u in result["used_with_rename"]
        if u["column"] == "A1_LOJA"
        for ev in u["renamed_in"]
    }
    aliases_for_pessoa = {
        ev["alias"]
        for u in result["used_with_rename"]
        if u["column"] == "A1_PESSOA"
        for ev in u["renamed_in"]
    }
    assert aliases_for_cod == {"KEY_CLIENTE"}
    assert aliases_for_loja == {"KEY_CLIENTE"}
    assert aliases_for_pessoa == {"TIPO_PESSOA"}

    # The remaining 17 columns must be reported as unused.
    assert len(result["unused_columns"]) == 17
    assert "A1_COD" not in result["unused_columns"]
    assert "A1_LOJA" not in result["unused_columns"]
    assert "A1_PESSOA" not in result["unused_columns"]
    assert "A1_SUFRAMA" in result["unused_columns"]
    assert "A1_DTCAD" in result["unused_columns"]


# ---------------------------------------------------------------------------
# Staleness detection
# ---------------------------------------------------------------------------

_ACT = datetime(2025, 6, 1, tzinfo=timezone.utc)   # lineage activation date
_BEFORE = datetime(2025, 5, 1, tzinfo=timezone.utc) # reloaded before activation → stale
_AFTER  = datetime(2025, 7, 1, tzinfo=timezone.utc) # reloaded after  activation → fresh


class TestParseActivationDate:
    def test_date_only_string(self):
        dt = _parse_activation_date("2025-06-01")
        assert dt == datetime(2025, 6, 1, tzinfo=timezone.utc)

    def test_datetime_with_z(self):
        dt = _parse_activation_date("2025-06-01T10:30:00Z")
        assert dt is not None
        assert dt.year == 2025 and dt.month == 6 and dt.day == 1

    def test_datetime_with_offset(self):
        dt = _parse_activation_date("2025-06-01T00:00:00+00:00")
        assert dt == datetime(2025, 6, 1, tzinfo=timezone.utc)

    def test_none_returns_none(self):
        assert _parse_activation_date(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_activation_date("") is None

    def test_invalid_string_returns_none(self):
        assert _parse_activation_date("not-a-date") is None


async def test_stale_consumer_is_skipped_and_surfaced(fake_client: FakeQlikClient):
    """A consumer reloaded before lineage activation must appear in
    stale_consumer_apps and must NOT trigger a field-level lineage call
    (FakeQlikClient raises for unknown graphs — if it were called, the test
    would error)."""
    target = _setup_file(fake_client, ["ColA", "ColB"])
    stale_app = App(id="STALE", name="Old App", usage="ANALYTICS", last_reload_time=_BEFORE)
    fake_client.apps_in_tenant = [stale_app]
    fake_client.app_fields = {"STALE": [AppField(name="ColA")]}
    fake_client.app_lineage = {
        "STALE": [LineageEntry(discriminator=f"lib://X:DataFiles/{target.name};")],
    }
    # Deliberately do NOT register a consumer lineage graph — if the code
    # tried to fetch it, FakeQlikClient would raise and the test would fail.

    result = await analyze_unused_columns(
        fake_client, target.name, "Finance",
        lineage_activation_date="2025-06-01",
    )

    assert result["summary"]["consumer_apps_found"] == 1
    assert result["summary"]["stale_consumer_apps_skipped"] == 1
    assert result["summary"]["consumer_lineage_extracted"] == 0
    stale = result["stale_consumer_apps"]
    assert len(stale) == 1
    assert stale[0]["app_id"] == "STALE"
    assert "2025-05-01" in stale[0]["last_reload_time"]
    assert "STALE" in result["recommendation"]["conditional_on_apps_we_could_not_inspect"]
    assert any("stale_consumer_apps" in d for d in result["disclaimers"])


async def test_fresh_consumer_processed_normally(fake_client: FakeQlikClient):
    """A consumer reloaded AFTER activation is treated as fresh — its
    field-level lineage is queried and renames are detected."""
    target = _setup_file(fake_client, ["ColA", "ColB"])
    fresh_app = App(id="FRESH", name="New App", usage="ANALYTICS", last_reload_time=_AFTER)
    fake_client.apps_in_tenant = [fresh_app]
    fake_client.app_fields = {"FRESH": []}
    fake_client.app_lineage = {
        "FRESH": [LineageEntry(discriminator=f"lib://X:DataFiles/{target.name};")],
    }
    app_qri = "qri:app:sense://FRESH"
    consumer_graph = LineageGraph(
        graph_type="FIELD",
        nodes=[
            LineageNode(qri=f"{FILE_QRI}#t#0", label="ColA", type="FIELD"),
            LineageNode(qri=f"{app_qri}#x#0", label="alias_a", type="FIELD"),
        ],
        edges=[
            LineageEdge(relation="from", source=f"{FILE_QRI}#t#0", target=f"{app_qri}#x#0"),
        ],
    )
    fake_client.lineage_graphs[(app_qri, "field")] = consumer_graph

    result = await analyze_unused_columns(
        fake_client, target.name, "Finance",
        lineage_activation_date="2025-06-01",
    )

    assert result["summary"]["stale_consumer_apps_skipped"] == 0
    assert result["stale_consumer_apps"] == []
    assert result["summary"]["consumer_lineage_extracted"] == 1
    renamed_cols = {u["column"] for u in result["used_with_rename"]}
    assert "ColA" in renamed_cols
    assert result["unused_columns"] == ["ColB"]


async def test_mixed_stale_and_fresh_consumers(fake_client: FakeQlikClient):
    """With two consumers — one stale, one fresh — the stale one is skipped
    and the fresh one provides rename evidence. The stale app appears in
    conditional_on_apps_we_could_not_inspect."""
    target = _setup_file(fake_client, ["ColA", "ColB", "ColC"])
    stale = App(id="STALE", name="Old App", usage="ANALYTICS", last_reload_time=_BEFORE)
    fresh = App(id="FRESH", name="New App", usage="ANALYTICS", last_reload_time=_AFTER)
    fake_client.apps_in_tenant = [stale, fresh]
    fake_client.app_fields = {
        "STALE": [AppField(name="ColA")],
        "FRESH": [AppField(name="ColB")],
    }
    entry = [LineageEntry(discriminator=f"lib://X:DataFiles/{target.name};")]
    fake_client.app_lineage = {"STALE": entry, "FRESH": entry}

    fresh_qri = "qri:app:sense://FRESH"
    fake_client.lineage_graphs[(fresh_qri, "field")] = LineageGraph(
        graph_type="FIELD", nodes=[], edges=[]
    )

    result = await analyze_unused_columns(
        fake_client, target.name, "Finance",
        lineage_activation_date="2025-06-01",
    )

    assert result["summary"]["stale_consumer_apps_skipped"] == 1
    assert result["summary"]["consumer_lineage_extracted"] == 1
    stale_ids = {a["app_id"] for a in result["stale_consumer_apps"]}
    assert stale_ids == {"STALE"}
    # ColB matched FRESH's metadata directly; ColA matched STALE (direct,
    # metadata is still checked for stale apps? No — stale apps skip
    # field-level lineage only. Direct metadata is NOT collected for stale
    # apps because we skip them in the fresh_consumer_apps loop.
    # ColC has no evidence from either consumer.
    assert "STALE" in result["recommendation"]["conditional_on_apps_we_could_not_inspect"]


async def test_no_activation_date_skips_staleness_check(fake_client: FakeQlikClient):
    """Without lineage_activation_date, stale detection is disabled — all
    consumer apps are processed regardless of last_reload_time."""
    target = _setup_file(fake_client, ["ColA"])
    app = App(id="OLD", name="Old App", usage="ANALYTICS", last_reload_time=_BEFORE)
    fake_client.apps_in_tenant = [app]
    fake_client.app_fields = {"OLD": [AppField(name="ColA")]}
    fake_client.app_lineage = {
        "OLD": [LineageEntry(discriminator=f"lib://X:DataFiles/{target.name};")],
    }
    app_qri = "qri:app:sense://OLD"
    fake_client.lineage_graphs[(app_qri, "field")] = LineageGraph(
        graph_type="FIELD", nodes=[], edges=[]
    )

    # No lineage_activation_date → backward-compatible, no stale detection.
    result = await analyze_unused_columns(fake_client, target.name, "Finance")

    assert result["summary"]["stale_consumer_apps_skipped"] == 0
    assert result["stale_consumer_apps"] == []
    assert result["summary"]["consumer_lineage_extracted"] == 1
