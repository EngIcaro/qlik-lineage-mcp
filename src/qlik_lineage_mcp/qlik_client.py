"""HTTP client for the Qlik Cloud REST API.

This is the **only** module in the project that talks to the network. Tools
work against the typed methods here, never raw ``httpx``. Centralizing
transport buys us:

- One place for auth headers, base URL, timeouts, and pagination.
- One place to add retries / rate-limit handling when needed.
- Tools that are trivial to unit-test by passing a fake client.

The client is ``async`` because FastMCP runs in an asyncio loop; using
``httpx.AsyncClient`` avoids blocking the event loop when fan-out across
many apps is needed (the unused_columns tool can easily hit hundreds
of apps in a tenant).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote, urlparse

import httpx

from .config import Settings
from .models import (
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

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Parsers — kept at module level so they can be unit-tested directly against
# fixtures without instantiating a client (which would need a tenant + key).
# -----------------------------------------------------------------------------

def parse_space(raw: dict[str, Any]) -> Space:
    """Build a ``Space`` from a raw ``/api/v1/spaces`` entry."""
    return Space.model_validate(raw)


def parse_app(raw: dict[str, Any]) -> App:
    """Build an ``App`` from a raw items entry of resourceType=app.

    The app id we care about is ``resourceId`` (a UUID), not the container
    item id. ``resourceAttributes.id`` is the same UUID — kept as fallback
    in case ``resourceId`` is missing in older API versions.
    """
    attrs = raw.get("resourceAttributes") or {}
    resource_id = raw.get("resourceId") or attrs.get("id", "")
    size = raw.get("resourceSize") or {}
    return App(
        id=resource_id,
        name=raw.get("name", ""),
        space_id=raw.get("spaceId"),
        app_file_size=int(size.get("appFile") or 0),
        app_memory_size=int(size.get("appMemory") or 0),
        usage=attrs.get("usage"),
    )


# ---------------------------------------------------------------------------
# Discriminator classification for ``/apps/{id}/data/lineage`` rows.
# Lives here (not in a tool) because multiple tools need it: ``ghost_files``
# uses it to build the app-file consumption graph; ``unused_columns`` uses
# it to find which apps consume a target file.
# ---------------------------------------------------------------------------

# Matches: ``{STORE - [lib://CONN:Folder/path/file.qvd](qvd)};``
_STORE_RE = re.compile(
    r"^\{STORE\s*-\s*\[(?P<path>[^\]]+)\]",
    re.IGNORECASE,
)

# Matches: ``lib://CONN:Folder/path/file.qvd;``
_LIB_PATH_RE = re.compile(r"^lib://", re.IGNORECASE)


def _basename(path: str) -> str:
    """Lowercased file-name component of a Qlik file reference.

    Inputs seen in real responses include the producer's STORE path
    (``lib://CONN:DataFiles/bronze_example.qvd``)
    and the consumer's LOAD path with different casing on the connection
    prefix. We normalize to the trailing basename so the comparison is
    independent of connection-prefix casing.
    """
    s = path.strip().rstrip(";")
    s = s.split("](", 1)[0]  # drop trailing ``](qvd)`` if present
    last = s.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return last.strip().lower()


def classify_discriminator(discriminator: str) -> tuple[str, str]:
    """Classify one ``data/lineage`` row.

    Returns ``(kind, file_name_lower)`` where ``kind`` is one of
    ``"load"``, ``"store"``, or ``"other"``. ``file_name_lower`` is empty
    when ``kind == "other"``.

    Other discriminator forms (RESIDENT/AUTOGENERATE/DB connections) are
    intentionally lumped into ``"other"`` because none of them carry a
    data-file dependency we can reason about.
    """
    s = (discriminator or "").strip()
    if not s:
        return ("other", "")
    m = _STORE_RE.match(s)
    if m:
        return ("store", _basename(m.group("path")))
    if _LIB_PATH_RE.match(s):
        return ("load", _basename(s))
    return ("other", "")


def parse_data_file(raw: dict[str, Any]) -> Optional[DataFile]:
    """Build a ``DataFile`` from a raw items entry.

    Returns ``None`` for non-file entries — notably ``resourceType=dataasset``
    (the parent ``DataFilesStore`` placeholder that the items endpoint
    returns alongside actual files).

    Format detection looks at ``resourceAttributes.type`` (``"qvd"`` in
    the fixtures). When a Parquet fixture is captured, confirm the field
    name and value here.
    """
    if raw.get("resourceType") != "dataset":
        return None
    attrs = raw.get("resourceAttributes") or {}
    file_type = str(attrs.get("type") or "").lower()
    if file_type == "qvd":
        fmt = FileFormat.QVD
    elif file_type == "parquet":
        # TODO: verify Parquet API shape when fixture is available.
        # Confirm that resourceAttributes.type is literally "parquet" and
        # that qri/secureQri follow the same convention as QVDs.
        fmt = FileFormat.PARQUET
    else:
        fmt = FileFormat.UNKNOWN
    size = raw.get("resourceSize") or {}
    return DataFile(
        name=raw.get("name", ""),
        space_id=raw.get("spaceId"),
        qri=attrs.get("qri"),
        secure_qri=attrs.get("secureQri"),
        format=fmt,
        estimated_size_bytes=int(size.get("appFile") or 0),
    )


def parse_app_field(raw: dict[str, Any]) -> AppField:
    """Build an ``AppField`` from a raw ``fields[]`` entry of data/metadata."""
    return AppField.model_validate(raw)


def parse_lineage_graph(payload: dict[str, Any]) -> LineageGraph:
    """Flatten the nested graph JSON into our ``LineageGraph`` model.

    Qlik returns ``graph.nodes`` as a dict keyed by QRI. We turn it into a
    flat list with the QRI promoted to a field, which is more convenient
    for iteration.
    """
    graph = payload.get("graph") or {}
    raw_nodes = graph.get("nodes") or {}
    raw_edges = graph.get("edges") or []
    nodes: list[LineageNode] = []
    for qri, node in raw_nodes.items():
        meta = node.get("metadata") or {}
        nodes.append(
            LineageNode(
                qri=qri,
                label=node.get("label", ""),
                type=meta.get("type"),
                subtype=meta.get("subtype"),
            )
        )
    edges = [
        LineageEdge(
            relation=e.get("relation", ""),
            source=e.get("source", ""),
            target=e.get("target", ""),
        )
        for e in raw_edges
    ]
    return LineageGraph(
        nodes=nodes,
        edges=edges,
        graph_type=graph.get("type", "RESOURCE"),
    )


# -----------------------------------------------------------------------------
# Client
# -----------------------------------------------------------------------------

class QlikClient:
    """Thin async wrapper around the Qlik Cloud REST API.

    Use as an async context manager so the underlying ``httpx`` connection
    pool is released::

        async with QlikClient(load_settings()) as client:
            spaces = await client.list_spaces()
    """

    def __init__(self, settings: Settings, http: Optional[httpx.AsyncClient] = None):
        self._settings = settings
        # Accepting an injected client makes testing trivial — tests can
        # pass an httpx.AsyncClient backed by respx / MockTransport.
        self._client = http or httpx.AsyncClient(
            base_url=settings.tenant_url,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Accept": "application/json",
            },
            timeout=settings.request_timeout_s,
        )

    async def __aenter__(self) -> "QlikClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ----- HTTP with 429 backoff ----------------------------------------

    async def _get_with_retry(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        max_retries: int = 5,
    ) -> "httpx.Response":
        """GET with exponential backoff on HTTP 429 Too Many Requests.

        Qlik Cloud throttles bursts of lineage calls. The server sometimes
        returns a ``Retry-After`` header (in seconds); we honor it when
        present, otherwise back off exponentially. After ``max_retries``
        the last 429 response is returned and the caller's
        ``raise_for_status()`` will surface it.
        """
        delay = 1.0
        for attempt in range(max_retries + 1):
            resp = await self._client.get(url, params=params)
            if resp.status_code != 429:
                return resp
            if attempt == max_retries:
                logger.warning(
                    "429 on %s — giving up after %d retries", url, max_retries
                )
                return resp
            retry_after_header = resp.headers.get("Retry-After")
            try:
                wait_s = float(retry_after_header) if retry_after_header else delay
            except ValueError:
                wait_s = delay
            wait_s = min(max(wait_s, 0.5), 30.0)
            logger.info(
                "429 on %s (attempt %d/%d), sleeping %.1fs",
                url, attempt + 1, max_retries + 1, wait_s,
            )
            await asyncio.sleep(wait_s)
            delay = min(delay * 2, 30.0)
        return resp  # unreachable, but keeps type checker happy

    # ----- pagination ---------------------------------------------------

    async def _paginate(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate every ``data[]`` item across all pages of a list endpoint.

        Qlik paginates with ``links.next.href``. The href returned is
        absolute — we strip the base URL so the next call still flows
        through the configured client (preserving auth, timeouts).
        """
        url: Optional[str] = path
        current_params = params
        base = str(self._client.base_url).rstrip("/")
        while url:
            resp = await self._get_with_retry(url, params=current_params)
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("data", []) or []:
                yield item
            next_href = (
                ((payload.get("links") or {}).get("next") or {}).get("href")
            )
            if not next_href:
                break
            # Next page's href is absolute and already carries query params,
            # so we must not re-pass ``params`` (it would double them).
            current_params = None
            if next_href.startswith(base):
                url = next_href[len(base):]
            else:
                # Different host — bail rather than silently follow an
                # unexpected redirect target.
                parsed = urlparse(next_href)
                logger.warning(
                    "Pagination next href points outside tenant base "
                    "(%s vs %s); stopping.",
                    parsed.netloc,
                    self._client.base_url.host,
                )
                break

    # ----- spaces -------------------------------------------------------

    async def list_spaces(self) -> list[Space]:
        """Return every space the API key has access to."""
        out: list[Space] = []
        async for raw in self._paginate("/api/v1/spaces"):
            out.append(parse_space(raw))
        return out

    async def find_space_by_name(self, name: str) -> Optional[Space]:
        """Case-insensitive lookup by space display name.

        We compare lowercased because Qlik space names are case-preserving
        but conventionally referenced case-insensitively in conversation
        (``"Finance"`` vs ``"finance"``).
        """
        target = name.strip().lower()
        async for raw in self._paginate("/api/v1/spaces"):
            space = parse_space(raw)
            if space.name.lower() == target:
                return space
        return None

    # ----- items (apps + data files) ------------------------------------

    async def list_apps_in_space(self, space_id: str) -> list[App]:
        """List apps in a given space (resourceType=app)."""
        out: list[App] = []
        async for raw in self._paginate(
            "/api/v1/items",
            params={"spaceId": space_id, "resourceType": "app"},
        ):
            out.append(parse_app(raw))
        return out

    async def list_apps_in_tenant(self) -> list[App]:
        """List every app across the tenant.

        Used by ``unused_columns`` because "not used" is a tenant-wide
        statement: a column can be used by an app in any space.
        """
        out: list[App] = []
        async for raw in self._paginate(
            "/api/v1/items",
            params={"resourceType": "app"},
        ):
            out.append(parse_app(raw))
        return out

    async def _fetch_data_file_sizes(self, space_id: str) -> dict[str, int]:
        """Return {name_lower: size_bytes} from /api/v1/data-files for a space.

        The items endpoint reports ``resourceSize.appFile == 0`` for QVDs.
        This endpoint carries the real on-disk sizes.
        """
        sizes: dict[str, int] = {}
        async for raw in self._paginate(
            "/api/v1/data-files",
            params={"spaceId": space_id},
        ):
            name = str(raw.get("name") or "").lower()
            size = int(raw.get("size") or 0)
            if name:
                sizes[name] = size
        return sizes

    async def list_data_files_in_space(self, space_id: str) -> list[DataFile]:
        """List data files in a space enriched with real sizes.

        Runs two calls in parallel:
        - ``/api/v1/items?resourceType=dataset`` — for QRIs and secureQRIs.
        - ``/api/v1/data-files`` — for real on-disk sizes (items returns 0 for QVDs).

        Sizes are merged by lowercased filename.
        """
        async def _collect_items() -> list[dict[str, Any]]:
            return [r async for r in self._paginate(
                "/api/v1/items",
                params={"spaceId": space_id, "resourceType": "dataset"},
            )]

        items_raw, sizes = await asyncio.gather(
            _collect_items(),
            self._fetch_data_file_sizes(space_id),
        )

        out: list[DataFile] = []
        for raw in items_raw:
            df = parse_data_file(raw)
            if df is not None:
                real_size = sizes.get(df.name.lower(), 0)
                if real_size > 0:
                    df = df.model_copy(update={"estimated_size_bytes": real_size})
                out.append(df)
        return out

    # ----- apps: metadata / script / lineage ----------------------------

    async def get_app_metadata(self, app_id: str) -> dict[str, Any]:
        """Return the raw ``/api/v1/apps/{appId}/data/metadata`` payload."""
        resp = await self._get_with_retry(f"/api/v1/apps/{app_id}/data/metadata")
        resp.raise_for_status()
        return resp.json()

    async def get_app_fields(
        self,
        app_id: str,
        include_system: bool = False,
    ) -> list[AppField]:
        """Return the user-visible fields of an app's data model.

        By default skips system (``is_system``) and hidden (``is_hidden``)
        fields — they would inflate "used" sets with synthetic things like
        ``$Table`` that are never the answer to "is this column used?".
        """
        meta = await self.get_app_metadata(app_id)
        out: list[AppField] = []
        for f in meta.get("fields", []) or []:
            field = parse_app_field(f)
            if not include_system and (field.is_system or field.is_hidden):
                continue
            out.append(field)
        return out

    async def get_app_lineage(self, app_id: str) -> list[LineageEntry]:
        """Return ``/api/v1/apps/{appId}/data/lineage`` rows.

        Each row's ``discriminator`` is a free-form string; the tools layer
        is responsible for parsing the patterns they care about.
        """
        resp = await self._get_with_retry(f"/api/v1/apps/{app_id}/data/lineage")
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            return []
        return [LineageEntry.model_validate(x) for x in payload]

    # ----- lineage graph ------------------------------------------------

    async def get_lineage_graph(
        self,
        qri: str,
        level: str = "resource",
    ) -> LineageGraph:
        """Fetch the lineage graph for a QRI.

        ``level='resource'`` (default) returns apps + datasets; pass
        ``level='field'`` for column-granularity lineage.

        The QRI contains ``:``, ``/``, and ``#`` characters that must be
        percent-encoded so they are not interpreted as path separators.
        """
        encoded = quote(qri, safe="")
        params = {"level": "field"} if level == "field" else None
        resp = await self._get_with_retry(
            f"/api/v1/lineage-graphs/nodes/{encoded}",
            params=params,
        )
        resp.raise_for_status()
        return parse_lineage_graph(resp.json())


# Built: ``qlik_client.py`` exposes pure parsers (testable against fixtures
# without a tenant) and a thin async ``QlikClient`` wrapping all REST calls.
# Assumptions to verify against the real tenant:
# - Pagination ``links.next.href`` shares the same host as the base URL
#   (we hard-stop if it does not — to avoid blindly following redirects).
# TODOs for Parquet:
# - Confirm that Parquet data files surface in ``/api/v1/items?resourceType=dataset``
#   with ``resourceAttributes.type='parquet'``. If they surface only via
#   ``/api/v1/data-files`` or a Data Lake connection, ``list_data_files_in_space``
#   may need a second call path that is then merged here.
