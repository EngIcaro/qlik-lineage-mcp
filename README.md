# Qlik Lineage MCP

A read-only [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server
that exposes data-file lineage analyses for [Qlik Cloud](https://www.qlik.com/us/products/qlik-cloud)
tenants. Built especially for tenants on the **capacity** pricing model
(daily peak GB), where dropping unused columns / ghost files yields direct savings.

## Tools

| Tool | What it answers |
|---|---|
| `unused_columns` | Given a data file (QVD or Parquet) and its space, which columns are not consumed by any app in the tenant? |
| `ghost_files`    | Given a space, which data files are not consumed by any app — including transitive chains (file -> file -> app)? |

Both tools are **read-only**. They recommend, they never delete.

## Quick start

```powershell
# 1. Install deps with uv (or pip)
uv sync

# 2. Copy and fill the env file
copy .env.example .env
# edit QLIK_TENANT_URL and QLIK_API_KEY

# 3. Run the MCP server (stdio transport)
uv run qlik-lineage-mcp
```

Wire the server into Claude Desktop / Claude Code / VS Code:

```json
{
  "mcpServers": {
    "qlik-lineage": {
      "command": "uv",
      "args": ["run", "qlik-lineage-mcp"],
      "cwd": "C:/path/to/qlik-lineage-mcp"
    }
  }
}
```

## Architecture

```
src/qlik_lineage_mcp/
├── server.py       # FastMCP entry point — auto-registers everything in tools/
├── config.py       # env-var loader (.env fallback)
├── qlik_client.py  # all Qlik Cloud HTTP calls live here
├── models.py       # format-agnostic Pydantic models (DataFile = QVD or Parquet)
└── tools/
    ├── __init__.py        # auto-discovers and calls register(mcp) on each module
    ├── unused_columns.py
    └── ghost_files.py
```

**Adding a new tool:** drop a file in `tools/` that exports `register(mcp: FastMCP)`.
`server.py` does not need to be edited.

## How `unused_columns` works

A 3-phase pipeline that uses Qlik's field-level lineage (no script parsing):

1. **Enumerate columns** — `GET /lineage-graphs/nodes/{file_qri}?level=field`
   exposes every column of the target file as a node whose QRI starts with
   the file QRI.
2. **Find consumer apps** — iterate every app in the tenant and read its
   `data/lineage`. Apps whose discriminators include a `lib://...{file_name}`
   LOAD reference are the consumers.
3. **Detect renames** — for each consumer app, fetch
   `GET /lineage-graphs/nodes/{app_qri}?level=field`. Edges whose source is
   a field of the target file map the original column to the alias used by
   the app. Qlik decomposes composite expressions automatically, so
   `LOAD A_COD & '\' & A_LOJA AS KEY FROM file` produces two edges
   (`A_COD->KEY`, `A_LOJA->KEY`) — no script parser needed.

Cost: one file-side call + N `data/lineage` calls (one per app, also paid
by `ghost_files`) + M field-level lineage calls (one per consumer app).

## Known limitations

- Rename detection requires the consumer app to have been **reloaded
  since field-level lineage was activated in the tenant**. Apps that have
  not been reloaded show no edges in their field-level graph, so renames
  in those apps are invisible. The output lists which consumer apps could
  not be inspected so the verdict is auditable.
- `ghost_files` walks every app's `data/lineage` and builds an app/file
  graph, then runs a fixpoint to mark useful chains. Dependencies hidden
  inside `SUB` / `CALL` / `$(include)` or dynamic file paths are missed.
- Apps whose `data/metadata` cannot be fetched (permissions, errors) are
  surfaced as a top-level `metadata_unavailable_apps` caveat — verdicts
  are conditional on those apps being checkable.
- Parquet support is implemented format-agnostically but until a real
  Parquet item-endpoint fixture is captured, surfacing of Parquet files
  is best-effort. The tools flag this in their output.

## Testing

```powershell
uv run pytest
```

All tests run against captured JSON fixtures in `tests/fixtures/` —
no live tenant calls.
