"""Tool: ``unused_columns`` (3-phase pipeline using field-level lineage).

Given a data file (QVD or Parquet) and the space it lives in, return the
list of its columns that are not consumed by any app in the tenant.

Pipeline:

1. **Enumerate file columns** via the file's own field-level lineage graph
   (``/lineage-graphs/nodes/{file_qri}?level=field``). The file's columns
   are the FIELD nodes whose QRI starts with the file QRI.

2. **Find consumer apps** by iterating every app's ``data/lineage`` and
   classifying its discriminator rows. A consumer is an app whose lineage
   carries a ``lib://...{file_name}`` LOAD reference. (Identical logic to
   the ``ghost_files`` tool — both use ``classify_discriminator``.)

3. **Extract rename evidence** from each consumer's own field-level
   lineage graph (``/lineage-graphs/nodes/{app_qri}?level=field``). Edges
   whose source is a field of our target file map the original column to
   the alias used by the consumer. The Qlik lineage explodes composite
   expressions automatically, so ``A1_COD & '\\' & A1_LOJA AS KEY_CLIENTE``
   appears as two edges (``A1_COD->KEY_CLIENTE`` and ``A1_LOJA->KEY_CLIENTE``).

A column is reported as **used** if it appears (case-insensitive) in any
app's metadata directly, or if any consumer's field-level lineage carries
a rename edge from it. Otherwise it is **unused** — safe to recommend
for removal, with the standard disclaimers.

Known blind spot: consumer apps that have not been reloaded since
field-level lineage was activated have no edges in their lineage graph,
so renames in those apps are invisible. The output surfaces the list of
consumer apps whose lineage graph could not be inspected, so the user can
treat the verdict as conditional on those apps.
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from ..config import load_settings
from ..models import App, AppField, DataFile, FileFormat, LineageGraph
from ..qlik_client import QlikClient, classify_discriminator

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lineage-graph helpers (pure functions; trivially unit-testable)
# ---------------------------------------------------------------------------

def _columns_from_field_graph(graph: LineageGraph, file_qri: str) -> list[str]:
    """Return the file's own column labels in stable graph order.

    File-side field nodes have QRIs of the form
    ``<file_qri>#<tableHash>#<fieldHash>``. The prefix filter prevents us
    from accidentally picking up consumer-side fields that happen to be
    in the graph.
    """
    prefix = f"{file_qri}#"
    seen: dict[str, None] = {}  # insertion-ordered dedup
    for node in graph.nodes:
        if node.type == "FIELD" and node.qri.startswith(prefix) and node.label:
            seen.setdefault(node.label, None)
    return list(seen.keys())


def _extract_renames_from_consumer_graph(
    graph: LineageGraph,
    file_qri: str,
) -> list[tuple[str, str, str, str]]:
    """Pull ``(file_column_label, alias_label, target_qri, relation)`` tuples.

    A "rename edge" is any edge whose source is a field of our target
    file. Empirically the relation can be ``from`` (direct rename),
    ``read``, ``rename``, ``modify`` — we surface whatever Qlik returned
    so the user can interpret it.

    The Qlik lineage already decomposes composite expressions into one
    edge per input field, so callers do not need to parse expressions
    themselves.
    """
    file_prefix = f"{file_qri}#"
    nodes_by_qri = {n.qri: n for n in graph.nodes}
    out: list[tuple[str, str, str, str]] = []
    for edge in graph.edges:
        if not edge.source.startswith(file_prefix):
            continue
        src = nodes_by_qri.get(edge.source)
        tgt = nodes_by_qri.get(edge.target)
        if src is None or tgt is None or not src.label or not tgt.label:
            continue
        out.append((src.label, tgt.label, tgt.qri, edge.relation))
    return out


# ---------------------------------------------------------------------------
# Core analysis — pure async function. Tests pass a FakeQlikClient.
# ---------------------------------------------------------------------------

async def analyze_unused_columns(
    client: QlikClient,
    file_name: str,
    space_name: str,
) -> dict:
    """Run the unused-columns analysis and return a JSON-serializable dict."""
    # ---- Phase 0: resolve space + target file
    space = await client.find_space_by_name(space_name)
    if space is None:
        return _error(
            f"Space '{space_name}' not found in tenant.",
            file_name=file_name,
            space_name=space_name,
        )
    files = await client.list_data_files_in_space(space.id)
    target = next((f for f in files if f.name.lower() == file_name.lower()), None)
    if target is None:
        return _error(
            f"File '{file_name}' not found in space '{space.name}'.",
            file_name=file_name,
            space_name=space.name,
            hint="Lookup is case-insensitive, but the extension must match (e.g. '.qvd').",
        )
    # The lineage-graphs endpoints require the *secureQri* (hashed form like
    # ``qri:qdf:space://<hash>#<hash>``). The plaintext ``qri`` field from
    # /items (``qdf:qix-datafiles:tenant:sid@space:filename``) is for human
    # display only and returns HTTP 400 if used against /lineage-graphs.
    lineage_qri = target.secure_qri or target.qri
    if not lineage_qri:
        return _error(
            "Target file has no QRI; cannot query field-level lineage.",
            file_name=target.name,
            space_name=space.name,
        )

    # ---- Phase 1: enumerate file columns
    try:
        file_graph = await client.get_lineage_graph(lineage_qri, level="field")
    except Exception as exc:  # pragma: no cover — network failures
        return _error(
            f"Field-level lineage query failed for the target file: {exc}",
            file_name=target.name,
            space_name=space.name,
        )
    file_columns = _columns_from_field_graph(file_graph, lineage_qri)
    if not file_columns:
        return _error(
            (
                "Field-level lineage returned no columns for this file. "
                "Ensure lineage is enabled in the tenant and that an app "
                "has reloaded after activation."
            ),
            file_name=target.name,
            space_name=space.name,
        )

    # ---- Phase 2: find consumer apps via data/lineage ONLY.
    # We deliberately do NOT collect metadata from every app in the
    # tenant — doing so would inflate "used" verdicts because a column
    # name can appear in any app for unrelated reasons (test apps,
    # backups, apps that load a similarly-named field from a different
    # source, etc.). Direct name matches must come from apps that
    # provably consume this file.
    apps = await client.list_apps_in_tenant()
    consumer_apps: list[App] = []
    lineage_failures: list[dict] = []  # data/lineage failures across the tenant
    target_name_lower = target.name.lower()

    for app in apps:
        try:
            entries = await client.get_app_lineage(app.id)
        except Exception as exc:
            logger.warning(
                "data/lineage failed for app %s (%s): %s",
                app.id, app.name, exc,
            )
            lineage_failures.append({"app_id": app.id, "app_name": app.name})
            continue
        for entry in entries:
            kind, fname = classify_discriminator(entry.discriminator)
            if kind == "load" and fname == target_name_lower:
                consumer_apps.append(app)
                break

    # ---- Phase 3: for CONSUMERS ONLY, pull (a) metadata for direct
    # field-name evidence and (b) field-level lineage for rename
    # evidence. Both signals are tied to a specific consumer app so the
    # response can show *which app* provides the evidence.
    direct_evidence: dict[str, list[dict]] = {}    # col_lower -> [{app_id, app_name}]
    rename_evidence: dict[str, list[dict]] = {}    # col_lower -> [{app_id, app_name, alias, relation}]
    metadata_failures: list[dict] = []             # consumers whose metadata errored
    consumer_lineage_failures: list[dict] = []     # consumers whose field-level lineage errored
    consumer_metadata_count = 0
    consumer_lineage_count = 0

    for app in consumer_apps:
        # Metadata of this consumer.
        consumer_field_set: set[str] = set()
        try:
            app_fields = await client.get_app_fields(app.id, include_system=False)
            consumer_field_set = {f.name.lower() for f in app_fields}
            consumer_metadata_count += 1
        except Exception as exc:
            logger.warning(
                "Metadata fetch failed for consumer %s (%s): %s",
                app.id, app.name, exc,
            )
            metadata_failures.append({"app_id": app.id, "app_name": app.name})

        # Direct match: file column name appears verbatim in this
        # consumer's loaded data model.
        for col in file_columns:
            if col.lower() in consumer_field_set:
                direct_evidence.setdefault(col.lower(), []).append(
                    {"app_id": app.id, "app_name": app.name}
                )

        # Field-level lineage of this consumer — drives rename detection.
        try:
            consumer_graph = await client.get_lineage_graph(
                app.lineage_qri, level="field"
            )
            consumer_lineage_count += 1
        except Exception as exc:
            logger.warning(
                "Field-level lineage failed for consumer %s (%s): %s",
                app.id, app.name, exc,
            )
            consumer_lineage_failures.append(
                {"app_id": app.id, "app_name": app.name, "error": str(exc)}
            )
            continue

        renames = _extract_renames_from_consumer_graph(consumer_graph, lineage_qri)
        # Dedupe by (column, alias) inside the same consumer so multiple
        # internal table references collapse to one piece of evidence.
        seen_pairs: set[tuple[str, str]] = set()
        for col_label, alias_label, tgt_qri, relation in renames:
            key = (col_label.lower(), alias_label.lower())
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            rename_evidence.setdefault(col_label.lower(), []).append(
                {
                    "app_id": app.id,
                    "app_name": app.name,
                    "alias": alias_label,
                    "relation": relation,
                }
            )

    # ---- Phase 4: categorize each column. Direct match takes priority
    # over rename because it is a stronger signal (alias === original).
    used_direct: list[dict] = []
    used_with_rename: list[dict] = []
    unused: list[str] = []
    for col in file_columns:
        col_lower = col.lower()
        if col_lower in direct_evidence:
            used_direct.append(
                {"column": col, "used_in": direct_evidence[col_lower]}
            )
            continue
        if col_lower in rename_evidence:
            used_with_rename.append(
                {"column": col, "renamed_in": rename_evidence[col_lower]}
            )
            continue
        unused.append(col)

    # ---- Phase 5: response
    parquet_warning = target.format == FileFormat.PARQUET
    has_lineage_failures = bool(consumer_lineage_failures)

    return {
        "file": {
            "name": target.name,
            "format": target.format.value,
            "qri": target.qri,
            "secure_qri": target.secure_qri,
        },
        "space": {"id": space.id, "name": space.name},
        "summary": {
            "total_columns": len(file_columns),
            "used_direct_count": len(used_direct),
            "used_with_rename_count": len(used_with_rename),
            "unused_count": len(unused),
            "apps_scanned": len(apps),
            "consumer_apps_found": len(consumer_apps),
            "consumer_metadata_extracted": consumer_metadata_count,
            "consumer_lineage_extracted": consumer_lineage_count,
        },
        "unused_columns": sorted(unused, key=str.lower),
        "used_direct": used_direct,
        "used_with_rename": used_with_rename,
        # Global caveats: any unused-column verdict is conditional on these.
        "metadata_unavailable_apps": metadata_failures,
        "lineage_unavailable_apps": lineage_failures,
        "consumer_lineage_failures": consumer_lineage_failures,
        "disclaimers": _disclaimers(
            parquet_warning,
            bool(metadata_failures),
            has_lineage_failures,
        ),
        # Per safety rule: never recommend removing a column whose status
        # is conditional on apps we could not inspect.
        "recommendation": {
            "safe_to_review_for_removal": sorted(unused, key=str.lower),
            "conditional_on_apps_we_could_not_inspect": (
                [a["app_id"] for a in metadata_failures]
                + [a["app_id"] for a in consumer_lineage_failures]
            ),
        },
    }


def _disclaimers(
    parquet_warning: bool,
    has_metadata_failures: bool,
    has_consumer_lineage_failures: bool,
) -> list[str]:
    """Disclaimers always attached to the response.

    Listed every time (not deduplicated) so an LLM consuming this output
    cannot silently drop them.
    """
    items = [
        "Used-direct and rename evidence are both scoped to *consumer apps* "
        "(those whose data/lineage shows a LOAD of this file). Apps that "
        "happen to have similarly-named fields from other sources are not "
        "counted as evidence — this avoids false 'used' verdicts.",
        "Rename detection relies on the consumer app's field-level lineage. "
        "Consumer apps that were not reloaded since field-level lineage was "
        "activated in the tenant produce no edges, so renames done in those "
        "apps are not detected.",
        "data/lineage may miss dependencies hidden inside SUB/CALL/$(include) "
        "or dynamic file paths, so a column could be referenced through "
        "indirection without being flagged as used.",
        "Recommendations are read-only suggestions. Validate against a "
        "known case before acting on a production tenant.",
    ]
    if has_metadata_failures:
        items.append(
            "Some apps could not be inspected (see metadata_unavailable_apps). "
            "Treat the unused list as conditional on those apps."
        )
    if has_consumer_lineage_failures:
        items.append(
            "Some consumer apps' field-level lineage could not be fetched "
            "(see consumer_lineage_failures). Renames in those apps are not "
            "represented in the result."
        )
    if parquet_warning:
        items.append(
            "Parquet support is pending real API-shape confirmation; "
            "treat results as best-effort for Parquet files until validated."
        )
    return items


def _error(message: str, **context) -> dict:
    """Build a consistent error response so the caller can render it nicely."""
    return {
        "error": message,
        "context": context,
        "disclaimers": _disclaimers(False, False, False),
    }


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------

def register(mcp: "FastMCP") -> None:
    """Register the ``unused_columns`` tool with the FastMCP server."""

    @mcp.tool()
    async def unused_columns(file_name: str, space_name: str) -> dict:
        """Return columns of a data file that no app in the tenant consumes.

        Args:
            file_name: Data file name with extension (e.g. ``Sales.qvd``).
                       Case-insensitive but extension must match.
            space_name: Display name of the space the file lives in.
                        Case-insensitive.
        """
        settings = load_settings()
        async with QlikClient(settings) as client:
            return await analyze_unused_columns(client, file_name, space_name)


# Built: 3-phase ``unused_columns`` — file-side enumeration via field-level
# lineage, consumer discovery via data/lineage, rename detection via each
# consumer's own field-level lineage. The Qlik lineage decomposes composite
# expressions automatically (``A1_COD & '\\' & A1_LOJA AS KEY_CLIENTE`` -> two
# edges), so no script parsing is required.
# Assumptions to verify against the real tenant:
# - Consumer app QRI is ``qri:app:sense://<id>`` when ``usage='ANALYTICS'``
#   and ``qri:app:dataprep://<id>`` when ``usage='DATA_PREPARATION'``.
#   Validated against the E-SHOP Sales fixture.
# - Edges with source = file field and target = app field carry one of
#   ``from``, ``read``, ``rename``, or ``modify`` as relation. We store
#   the relation as evidence; we do not filter by it because more relation
#   values may exist in tenants we have not sampled.
# TODOs for Parquet:
# - When a Parquet fixture exists, verify that consumer apps' field-level
#   lineage uses the same edge shape (file field -> app field with one of
#   the known relations) when the file is Parquet.
