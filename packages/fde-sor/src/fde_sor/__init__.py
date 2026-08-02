"""fde_sor -- system-of-record adapters for the FDE platform.

This package is what makes drift detection possible at all. `db/007_drift.sql`
defines `sor.observation` ("how work is actually done") and four detectors that
compare it against the knowledge graph ("how work is asserted to be done"), but
until now nothing in the repo wrote a single observation row -- the detectors
ran against an empty table and correctly found nothing.

A fourth workspace member rather than more modules inside `fde-mcp`, because
the split in this repo is along deployment boundaries (docs/11 §2). These
adapters run as Lambdas and Kubernetes CronJobs under the `fde_ingest` role
holding per-customer SoR credentials from Secrets Manager -- a different
credential set, blast radius, and lifecycle than the always-on MCP service
running as `fde_agent`. They reuse `fde_mcp.db`, `fde_mcp.config.DatabaseSettings`
and `fde_mcp.logging` (exactly as `fde_mcp.embedder_worker` and
`fde_training.config` do) rather than redeclaring any of it.

The shape of the thing
-----------------------
Every adapter kind implements exactly one method -- `fetch(cursor)`, an async
iterator of raw SoR records. Everything after that (mapping through
`sor.adapter.mapping`, salted actor hashing, idempotent insert, cursor advance)
lives once in `observations.ingest`. That is deliberate: the `kind` column
selects only how records are obtained, never how they are interpreted
(docs/08 §2.4), so a fifth kind is a new `fetch` and nothing else.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.2.0"
