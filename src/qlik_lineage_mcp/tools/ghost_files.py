"""Tool: ``ghost_files``.

Given a space, return the data files (QVD/Parquet/...) that are not
consumed as a source by any app in the tenant — including chain ghosts
(files only loaded by an app whose own output files are also ghosts).

Algorithm:

1. List the files in the space (the candidate set).
2. Iterate every app in the tenant and read its ``data/lineage``. Each
   row's ``discriminator`` is classified as a LOAD source, a STORE sink,
   or "other" (DB connection, RESIDENT, AUTOGENERATE, ...).
3. Build a bipartite graph between apps and files:
       LOAD  : file -> app   (the app consumes the file)
       STORE : app  -> file  (the app produces the file)
4. Compute the **useful** set via a fixpoint:
     - Bootstrap "useful apps" = apps that consume at least one file
       *and* produce nothing (the leaf consumers — typically analytics
       apps that surface dashboards).
     - Files consumed by useful apps become useful.
     - Apps that produce a useful file become useful (intermediate
       data-prep apps in the chain are kept alive by their downstream).
     - Repeat until nothing changes.
6. Ghosts = files in the space that are not in the useful set. Estimated
   GB gain is the sum of their reported sizes (best-effort — the items
   endpoint reports 0 for many QVDs; we surface that limitation).
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from ..config import load_settings
from ..models import App, DataFile, FileFormat, LineageEntry
from ..qlik_client import QlikClient, classify_discriminator

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core analysis — pure, takes a client so tests can pass a fake one.
# ---------------------------------------------------------------------------

async def _build_app_file_graph(
    client: QlikClient,
    apps: list[App],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return ``(consumes, produces)`` maps from app id to file basenames.

    Iterates every app and reads its ``data/lineage``. Each row is
    classified and the bipartite graph is built up. Apps whose lineage
    endpoint errors are skipped with a warning — a single broken app must
    not abort the whole analysis.
    """
    consumes: dict[str, set[str]] = {}
    produces: dict[str, set[str]] = {}
    for app in apps:
        try:
            entries = await client.get_app_lineage(app.id)
        except Exception as exc:
            logger.warning(
                "Skipping lineage for app %s (%s): %s", app.id, app.name, exc
            )
            continue
        for entry in entries:
            kind, fname = classify_discriminator(entry.discriminator)
            if not fname:
                continue
            if kind == "load":
                consumes.setdefault(app.id, set()).add(fname)
            elif kind == "store":
                produces.setdefault(app.id, set()).add(fname)
    return consumes, produces


def compute_useful_files(
    apps: list[App],
    consumes: dict[str, set[str]],
    produces: dict[str, set[str]],
) -> set[str]:
    """Fixpoint that marks files / apps as 'useful'.

    Useful files are kept (not ghosts). A file is useful iff some useful
    app consumes it. An app is useful iff it has at least one consumed
    file and either produces nothing (leaf analytics consumer) or
    produces at least one useful file (intermediate prep that feeds the
    chain).
    """
    useful_files: set[str] = set()
    useful_apps: set[str] = set()

    # Bootstrap: leaf consumers — apps that consume at least one file but
    # do not store any. These are the canonical "final" apps; without them
    # nothing else upstream has a reason to exist.
    for app in apps:
        consumed = consumes.get(app.id, set())
        produced = produces.get(app.id, set())
        if consumed and not produced:
            useful_apps.add(app.id)
            useful_files |= consumed

    # Iterate to a fixpoint: each pass may discover new useful apps (because
    # one of their stored files just became useful) which then makes the
    # files they consume useful, and so on up the chain.
    changed = True
    while changed:
        changed = False
        for app in apps:
            if app.id in useful_apps:
                continue
            produced = produces.get(app.id, set())
            if produced & useful_files:
                useful_apps.add(app.id)
                new_files = consumes.get(app.id, set()) - useful_files
                if new_files:
                    useful_files |= new_files
                changed = True
        # A pass may also surface useful files first (for example when an
        # app consumes a file we just promoted): re-walk apps' consumes to
        # ensure we did not miss anything.
        for app_id in list(useful_apps):
            new_files = consumes.get(app_id, set()) - useful_files
            if new_files:
                useful_files |= new_files
                changed = True
    return useful_files


async def analyze_ghost_files(
    client: QlikClient,
    space_name: str,
) -> dict:
    """Run the ghost-files analysis and return a JSON-serializable dict."""
    space = await client.find_space_by_name(space_name)
    if space is None:
        return _error(
            f"Space '{space_name}' not found in tenant.",
            space_name=space_name,
        )
    files = await client.list_data_files_in_space(space.id)

    apps = await client.list_apps_in_tenant()
    consumes, produces = await _build_app_file_graph(client, apps)
    useful_files = compute_useful_files(apps, consumes, produces)

    ghosts: list[dict] = []
    for df in files:
        if df.name.lower() in useful_files:
            continue
        ghosts.append(
            {
                "name": df.name,
                "format": df.format.value,
                "qri": df.qri,
                "estimated_size_bytes": df.estimated_size_bytes,
                "estimated_gb_gain": _bytes_to_gb(df.estimated_size_bytes),
            }
        )

    total_bytes = sum(g["estimated_size_bytes"] for g in ghosts)

    has_parquet = any(
        df.format == FileFormat.PARQUET for df in files
    )

    return {
        "space": {"id": space.id, "name": space.name},
        "summary": {
            "ghost_count": len(ghosts),
            "total_files_in_space": len(files),
            "estimated_total_size_bytes": total_bytes,
            "estimated_total_gb_gain": _bytes_to_gb(total_bytes),
            "apps_scanned": len(apps),
        },
        "ghost_files": sorted(ghosts, key=lambda g: g["name"].lower()),
        "disclaimers": _disclaimers(has_parquet),
        # Per safety rule: always surface the estimated GB gain so the user
        # can weigh the capacity impact before acting.
        "recommendation": {
            "safe_to_review_for_removal": [g["name"] for g in ghosts],
            "estimated_total_gb_gain": _bytes_to_gb(total_bytes),
            "note": (
                "Sizes come from /api/v1/data-files and reflect on-disk "
                "bytes at scan time. Files deleted between the two API "
                "calls will show 0 bytes."
            ),
        },
    }


def _bytes_to_gb(b: int) -> float:
    """Convert bytes to gibibytes with 4 decimal places."""
    if b <= 0:
        return 0.0
    return round(b / (1024 ** 3), 4)


def _disclaimers(has_parquet_in_space: bool) -> list[str]:
    items = [
        "Lineage from /apps/{id}/data/lineage may miss dependencies "
        "introduced by SUB/CALL/$(include) or by dynamic file paths.",
        "Chain ghosts assume that an app with no STORE outputs is a final "
        "consumer. Apps that publish results via channels other than file "
        "stores (e.g., reports, alerts) are still treated as final.",
        "Recommendations are read-only suggestions. Validate against a "
        "known case before acting on a production tenant.",
    ]
    if has_parquet_in_space:
        items.append(
            "Parquet support is pending real API-shape confirmation; "
            "Parquet files may be under- or over-counted until validated."
        )
    return items


def _error(message: str, **context) -> dict:
    return {"error": message, "context": context, "disclaimers": _disclaimers(False)}


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------

def register(mcp: "FastMCP") -> None:
    """Register the ``ghost_files`` tool with the FastMCP server."""

    @mcp.tool()
    async def ghost_files(space_name: str) -> dict:
        """Return data files in a space that no app in the tenant consumes.

        Args:
            space_name: Display name of the space to scan. Case-insensitive.
        """
        settings = load_settings()
        async with QlikClient(settings) as client:
            return await analyze_ghost_files(client, space_name)


# Built: ``ghost_files`` tool with chain detection via a fixpoint over the
# app/file bipartite graph derived from ``data/lineage`` entries.
# Assumptions to verify:
# - Treating "app with no STORE outputs" as a final useful consumer is the
#   right bootstrap. A Qlik tenant where every app stores something would
#   leave the useful set empty until the fixpoint promotes intermediate
#   stores — that case is handled but should be validated against a known
#   tenant.
# - ``estimated_size_bytes`` from the items endpoint is 0 for QVDs in our
#   fixtures. A future revision should pull real sizes from
#   ``/api/v1/data-files`` to make the GB-gain estimate accurate.
# TODOs for Parquet:
# - Confirm that Parquet files surface as ``resourceType=dataset`` in the
#   items endpoint and that their STORE/LOAD references in ``data/lineage``
#   follow the same ``lib://CONN:DataFiles/...parquet`` convention. If
#   they use a different prefix, extend ``classify_discriminator``.
