# fde-mcp

The MCP server over the knowledge graph: 23 typed tools, the embedder worker,
and the shared `config`/`db`/`logging` modules every other package reuses.
This is the only artifact that holds database credentials, which is why its
dependency set is the smallest in the workspace.

This file is the package README that `docs/05-mcp-surface.md`,
`docs/99-sources.md`, and module docstrings cite. Tool-by-tool contracts live
in `docs/05`; this file covers the roster, the deployment mapping, and the
package's known limitations at their current truth.

## Tool roster (23)

| Group | Tools |
|---|---|
| Graph reads (9) | `kg_head_commit`, `kg_as_of`, `kg_search`, `kg_lexical_search`, `kg_get_node`, `kg_traverse`, `kg_dependency_closure`, `kg_impact_radius`, `kg_process_flow` |
| Proposals (3) | `kg_propose`, `kg_submit_proposal`, `kg_proposal_status` |
| Drift (3) | `drift_list`, `drift_scan`, `drift_triage` |
| Workflow (4) | `wf_draft`, `wf_get`, `wf_list`, `wf_export_playbook` |
| Evidence (3) | `kg_register_source`, `kg_ingest_chunks`, `kg_list_sources` |
| Dashboard (1) | `hitl_export_dashboard` |

Every tool is a thin typed wrapper over SQL in `db/008_retrieval.sql` and
friends — no retrieval, gating, or faithfulness logic is reimplemented in
Python, so production inference, RL rollouts, and `psql` see byte-identical
behaviour (`packages/fde-training/tests/test_parity.py` enforces this).

Chunk ingestion note: `kg_ingest_chunks` warns loudly when a chunk arrives
with empty `anchor_keys`, because `kg.hybrid_search`'s chunk arm only fuses
chunks with at least one anchor — an unanchored chunk is embedded but
invisible to retrieval.

## Mapping to AgentCore: Gateway vs. in-runtime MCP

Two ways an agent reaches these tools; the code paths differ only in
transport (`fde_agents.common.mcp_tools.build_mcp_client` picks by config):

| | AgentCore Gateway | In-runtime MCP |
|---|---|---|
| Path | agent → Gateway (`mcpServer` target → this server over streamable HTTP; `lambda` target → `fde-sor-backfill`) | agent container spawns `python -m fde_mcp` over stdio, or points at a colocated HTTP instance |
| Auth | Gateway-minted JWT (CUSTOM_JWT / Cognito); server never sees AWS creds | none added — process boundary or network policy is the boundary |
| When | production: one shared tool surface, centrally throttled and audited | local dev and tests (`FDE_MCP_TRANSPORT=stdio`), or a single dedicated agent |
| Cost | one extra network hop; Gateway target limits apply | none, but each runtime carries DB credentials |
| Provisioning | `fde-agents-deploy gateway …` (`deploy/gateway.py`) | nothing — the default `FDE_MCP_SERVER_ARGS="-m fde_mcp"` works in any checkout/image |

The batch-ingest escape hatch (`sor_backfill_observations`) is a real Lambda
(`fde-sor`) with its packaged schema shipped at
`fde_agents/deploy/schemas/sor_backfill_tool_schema.json`; a contract test
pins schema ↔ handler agreement.

## What was stubbed versus verified

`embeddings.py` (Titan v2 and Cohere v4 request/response shapes, both Cohere
response variants) is written against the documented Bedrock API shapes
recorded in `docs/99-sources.md` §3 and exercised in tests with the network
call stubbed (`tests/conftest.py`). It has NOT been run against a live
Bedrock endpoint from this repo. Everything else in the package — SQL,
transactions, role downgrades, the embed queue, all 23 tools — runs against
live Postgres in `tests/` (28+ `requires_db` cases) and in the smoke suite.

## Known limitations (current truth)

- `drift_scan` requires no special ownership: `sor.run_all_detectors` is
  `SECURITY DEFINER` (matview refresh ownership) and `fde_agent` holds
  EXECUTE since `db/011`. Older notes describing this as broken "for ANY
  role" are obsolete — `tests/test_drift_tools.py` now fails if the scan
  errors.
- The embedder worker processes `node`, `edge`, and `chunk` subjects. Chunks
  are embedded from `kg.chunk.content` verbatim; there is no chunk
  re-embedding on source edit because chunks are immutable by grant
  (INSERT-only for `fde_agent`) — re-chunk under a new source instead.
- `kg.embed_queue`'s uniqueness is `(subject_kind, subject_id)` — global,
  not per-engagement. Fine for identity ids; do not repurpose the queue for
  natural keys.
- HNSW probes must keep the double `::halfvec(1024)` cast (expression
  indexes); dropping one silently degrades to a sequential scan.
- `fde-embedder` (the worker's console script) has its deployment described
  in `docs/09-deployment.md` §7; the EKS manifests under `infra/k8s/` cover
  the fde-sor jobs, while the worker runs wherever the MCP service runs.

## Test plan

`FDE_DB_DSN=postgresql:///<db> uv run pytest packages/fde-mcp` after
`./db/rebuild.sh <db>`. Live-DB coverage: all 23 tools (including the
MCP-schema rejection paths via `mcp.call_tool`), the worker's claim/embed/
fail flows with the Bedrock call stubbed, config and logging contracts.
Without a DSN the db-marked cases skip and the pure cases still run.
