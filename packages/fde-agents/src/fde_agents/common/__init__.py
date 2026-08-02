"""Shared building blocks for the FDE Platform's three AgentCore runtimes
(Engagement, Workflow, Development).

Nothing in this package talks to Postgres for graph/HITL/drift/workflow
state directly. That state is reached exclusively through the MCP tool
surface `fde_mcp.server` exposes (see `mcp_tools.py`), which is itself a
thin wrapper over `db/004_hitl_gates.sql`, `db/006_workflows.sql`,
`db/007_drift.sql` and `db/008_retrieval.sql`. Keeping that boundary intact
here -- rather than, say, caching a psycopg pool in an agent process for
convenience -- is what lets "agents propose, humans dispose" hold
structurally instead of by convention: an agent process that never opens a
database connection of its own cannot accidentally acquire a role with
write access to `kg.node`/`kg.edge`.

The one deliberate, narrow exception is `mcp_tools.raw_sql`, used only by
`tracing.py` to write the `trn.trace_session`/`trn.trace_step` rows the MCP
server's own tool-call tracing never sees (the user/assistant turns, not
the tool turns) -- see that function's docstring for exactly why and how it
is bounded.
"""

from __future__ import annotations
