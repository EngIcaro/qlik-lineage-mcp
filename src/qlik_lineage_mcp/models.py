"""Pydantic models for the Qlik Cloud API surface we consume.

Design intent:
- ``DataFile`` is **format-agnostic**: it represents any data file (QVD,
  Parquet, future formats). Tools never branch on ``.qvd``; they branch on
  the ``format`` enum so that adding a new format is a one-line change here.
- We deliberately use ``extra="ignore"`` because Qlik responses include
  many fields we do not need (audit metadata, presentation hints, links).
  Ignoring them keeps the models stable when Qlik adds new attributes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class FileFormat(str, Enum):
    """Recognized data-file formats. ``UNKNOWN`` is set when the API value
    does not match a known type (e.g. unexpected ``resourceAttributes.type``).
    """

    QVD = "qvd"
    PARQUET = "parquet"  # TODO: verify Parquet API shape when fixture is available
    UNKNOWN = "unknown"


class Space(BaseModel):
    """A Qlik Cloud space (``/api/v1/spaces`` entry).

    We only carry the fields needed for routing and display. The Qlik
    response carries dozens of audit fields; ``extra='ignore'`` drops them.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    type: str  # "shared" | "managed" | "personal" | "data" | ...
    description: Optional[str] = None


class App(BaseModel):
    """A Qlik Sense app (``/api/v1/items`` entry with ``resourceType=app``).

    ``id`` is the Qlik app id (UUID), taken from ``resourceId``. The
    container item id (``raw['id']``) is intentionally not stored — every
    downstream Qlik endpoint we use keys off the app id, not the item id.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    space_id: Optional[str] = None
    app_file_size: int = 0     # bytes on disk (``resourceSize.appFile``)
    app_memory_size: int = 0    # bytes in memory (``resourceSize.appMemory``)
    # ``ANALYTICS`` (Sense dashboards) or ``DATA_PREPARATION`` (data prep apps).
    # Drives ``lineage_qri`` below because the Qlik lineage namespace uses
    # different prefixes for the two app types.
    usage: Optional[str] = None
    # Timestamp of the most recent successful reload. Used for staleness
    # detection: apps reloaded before field-level lineage was activated in the
    # tenant have no edges in their lineage graph and renames are invisible.
    last_reload_time: Optional[datetime] = None

    @property
    def lineage_qri(self) -> str:
        """QRI used in the ``/lineage-graphs/nodes/{qri}`` endpoint.

        Empirically: ``ANALYTICS`` apps are addressed as ``qri:app:sense://<id>``
        and ``DATA_PREPARATION`` apps as ``qri:app:dataprep://<id>``. When
        ``usage`` is missing we default to ``sense`` because dashboards are
        the more common consumer type the tools care about.
        """
        if (self.usage or "").upper() == "DATA_PREPARATION":
            return f"qri:app:dataprep://{self.id}"
        return f"qri:app:sense://{self.id}"


class DataFile(BaseModel):
    """Generic file in a space — QVD today, Parquet/other tomorrow.

    ``qri`` is the Qlik Resource Identifier needed for any lineage-graph
    query. It comes from ``resourceAttributes.qri`` in the items endpoint.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    space_id: Optional[str] = None
    qri: Optional[str] = None
    secure_qri: Optional[str] = None
    format: FileFormat = FileFormat.UNKNOWN
    # Real size from /api/v1/data-files; 0 when the endpoint has no entry for
    # this file (e.g. file was deleted between the two parallel calls).
    estimated_size_bytes: int = 0


class AppField(BaseModel):
    """A field/column inside an app's loaded data model.

    Comes from ``/api/v1/apps/{appId}/data/metadata`` ``fields[]`` entries.
    These are **post-rename**: if a script did ``LOAD X AS Y``, ``name='Y'``.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    src_tables: list[str] = Field(default_factory=list)
    is_system: bool = False
    is_hidden: bool = False
    byte_size: int = 0
    tags: list[str] = Field(default_factory=list)


class LineageEntry(BaseModel):
    """A single row from ``/api/v1/apps/{appId}/data/lineage``.

    ``discriminator`` encodes the source/sink as a free-form string. Examples
    seen in real responses:
      - ``"lib://space:datafiles/file.qvd;"``                 (read from QVD)
      - ``"{STORE - [lib://space:DataFiles/out.qvd](qvd)};"`` (write to QVD)
      - ``"{AF_Connections:db_name};"``                       (DB source)
      - ``"RESIDENT TABLE_NAME;"``                            (resident reload)
      - ``"AUTOGENERATE;"``                                   (synthetic)
    """

    model_config = ConfigDict(extra="ignore")

    discriminator: str = ""
    statement: str = ""


class LineageNode(BaseModel):
    """A node in a lineage graph (``/api/v1/lineage-graphs/nodes/{qri}``).

    ``type`` / ``subtype`` distinguish the kind of node:
      - type=``DA_APP``, subtype=``PROCESSOR`` -> an app
      - type=``DATASET``, subtype=``FILE``     -> a QVD/Parquet
      - type=``DATASET``, subtype=``TABLE``    -> a DB table
      - type=``FIELD``                         -> a column (field-level graph)
    """

    model_config = ConfigDict(extra="ignore")

    qri: str
    label: str
    type: Optional[str] = None
    subtype: Optional[str] = None


class LineageEdge(BaseModel):
    """An edge in the lineage graph.

    ``relation`` values observed: ``LOAD`` (file -> app), ``STORE`` (app -> file),
    ``read`` (field -> field).
    """

    model_config = ConfigDict(extra="ignore")

    relation: str
    source: str
    target: str


class LineageGraph(BaseModel):
    """Full lineage graph response.

    ``graph_type`` is either ``RESOURCE`` (apps and datasets) or ``FIELD``
    (individual columns). The semantics of edges differ between the two:
    at RESOURCE level the edges connect apps and files; at FIELD level they
    connect individual column nodes.
    """

    nodes: list[LineageNode]
    edges: list[LineageEdge]
    graph_type: str = "RESOURCE"

    def nodes_by_qri(self) -> dict[str, LineageNode]:
        """Convenience index for tools that need O(1) node lookup."""
        return {n.qri: n for n in self.nodes}


# Built: format-agnostic Pydantic models covering spaces, apps, data files,
# app fields, app-lineage entries, and the lineage graph.
# Assumptions to verify:
# - ``Space.type`` values: I saw ``shared`` and ``managed`` in the fixtures —
#   tools should not depend on a specific enum here, hence ``str`` instead of
#   ``Enum``.
# - ``DataFile.estimated_size_bytes`` is 0 for QVDs in the items endpoint;
#   the ghost_files tool will need a real size source eventually.
# TODOs for Parquet:
# - When a Parquet item-endpoint fixture exists, confirm that
#   ``resourceAttributes.type`` is literally ``"parquet"`` (case-insensitive
#   match is already in place) and that ``qri`` / ``secureQri`` exist with
#   the same shape as QVDs.
