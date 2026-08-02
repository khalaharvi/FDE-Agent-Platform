"""server.py -- the FDE Platform MCP server.

This module is deliberately thin: it builds one `FastMCP` instance, hands it
to `fde_mcp.tools.register_all` to attach the 21 tools, and picks a
transport. It does not reimplement any retrieval, gating, or faithfulness
logic in Python -- that logic lives in exactly one place (the database, see
db/008_retrieval.sql's module docstring) so that production inference, RL
rollout, and `psql` all see byte-identical behaviour.

Two deployment shapes, one codebase
------------------------------------
  * `FDE_MCP_TRANSPORT=stdio` (default) -- a local subprocess MCP server,
    talking JSON-RPC over stdin/stdout. This is what you get when the
    Engagement/Workflow/Development agent is wired to this server as an
    AgentCore Gateway target running in-process, or for local dev with an
    MCP Inspector / Claude Desktop style client.
  * `FDE_MCP_TRANSPORT=http` -- streamable-HTTP on 0.0.0.0:8080 at `/mcp`.
    This is the shape AgentCore Runtime requires for `protocol=MCP`: the
    runtime health-checks and proxies to exactly that host/port/path.

The security model
-------------------
The process connects to Postgres as an "owner-adjacent" principal (see
db.py), then every single tool call opens ONE transaction, sets a 20s
statement_timeout, and issues `SET LOCAL ROLE fde_agent` before touching any
table. `fde_agent` cannot write kg.node/kg.edge under any circumstances
(REVOKEd explicitly in 004/010) -- the only way graph facts change is
`hitl.propose -> hitl.submit_proposal -> (human gate) -> hitl.merge_proposal`,
and `merge_proposal` is not even reachable from this role. Every write this
server performs is either a proposal draft, a trace/observability row, or a
narrowly-scoped annotation (drift triage, a draft workflow) -- see
db/011_mcp_agent_supplemental_grants.sql for the two grants that had to be
added on top of 010's baseline agent role to make the drift-triage and
workflow-draft tools work, and why they don't weaken "agents propose, humans
dispose".

Error handling contract
------------------------
Every tool is wrapped by `tools._base.pg_error_boundary`. A Postgres error
that the model can plausibly fix by changing its own arguments -- a plpgsql
`RAISE EXCEPTION` (SQLSTATE P0001, e.g. "no gate policy matched"), or a
constraint violation (check/foreign-key/unique/not-null/invalid-uuid-syntax)
-- is re-raised as a plain exception so the MCP client sees the RAISE
message text verbatim and the model can self-correct. Anything else
(connection drop, permission/config problem, an unexpected server error) is
caught and returned as `{"error": ..., "hint": ...}` so a single flaky query
cannot tear down the whole tool call loop.

Training substrate
-------------------
When `FDE_TRACE_SESSION_ID` is set, every tool call appends one row to
`trn.trace_step` (role='tool', trainable=false, tool_result truncated to
8KB, and a populated `retrieval` JSON for the read tools). This happens in
a SAVEPOINT nested inside the tool's own transaction, so a tracing failure
(most commonly: the trace session row doesn't exist yet) never rolls back
the tool's actual work -- it is logged and swallowed. See
`tools._base.emit_trace`.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from fde_mcp import db, tools
from fde_mcp.config import VALID_TRANSPORTS, get_settings
from fde_mcp.logging import configure_logging, get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger(__name__)


@contextlib.asynccontextmanager
async def _lifespan(_server: FastMCP[None]) -> AsyncIterator[None]:
    await db.get_pool()  # fail fast if the DB is unreachable at startup
    try:
        yield
    finally:
        await db.close_pool()


def build_server() -> FastMCP[None]:
    """Construct the FastMCP instance and attach every tool.

    A function rather than only a module-level side effect so tests (and
    any future embedding of this server into a larger process) can build
    a fresh instance without reimporting the module.
    """
    server: FastMCP[None] = FastMCP(
        "fde-graph",
        instructions=(
            "Tools over the FDE knowledge graph: bitemporal process/role/system "
            "graph, hybrid ANN+graph retrieval, human-in-the-loop change "
            "proposals, drift monitoring against systems of record, and "
            "faithful workflow authoring. Call kg_head_commit first and pin its "
            "commit_id for the duration of a task. Every retrieval result "
            "carries a `provenance` field -- cite it, do not assert facts that "
            "are not present in a tool result. Agents cannot write the graph "
            "directly: kg_propose stages a change, hitl.submit_proposal freezes "
            "the required human reviewers, and only a human's approval merges it."
        ),
        host="0.0.0.0",  # noqa: S104 -- required by AgentCore Runtime's MCP health-check contract
        port=8080,
        lifespan=_lifespan,
    )
    tools.register_all(server)
    return server


mcp: FastMCP[None] = build_server()


def main() -> None:
    """Entrypoint for `python -m fde_mcp` and the `fde-mcp` console script."""
    configure_logging()
    transport = get_settings().transport
    if transport not in VALID_TRANSPORTS:
        raise SystemExit(
            f"FDE_MCP_TRANSPORT must be one of {VALID_TRANSPORTS!r}, got {transport!r}"
        )
    if transport == "http":
        log.info(
            "starting_mcp_server",
            transport="streamable-http",
            host=mcp.settings.host,
            port=mcp.settings.port,
            path=mcp.settings.streamable_http_path,
        )
        mcp.run(transport="streamable-http")
    else:
        log.info("starting_mcp_server", transport="stdio")
        mcp.run(transport="stdio")


__all__: list[str] = ["build_server", "main", "mcp"]
