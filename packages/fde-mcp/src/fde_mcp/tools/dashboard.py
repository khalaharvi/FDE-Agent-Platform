"""tools/dashboard.py -- the aggregate view of how decisions are flowing.

One tool, in its own module rather than folded into `proposals.py`, because
what it aggregates belongs to no single existing concern: it reads across
`hitl`, `kg`, `sor` and `wf` at once, and the module split here is by concern
with each module's test file mapping 1:1 to the tools it covers (docs/11 §6).
The document itself -- queries and renderers -- lives in `fde_mcp.dashboard`,
beside `fde_mcp.playbook`, because the console renders the same numbers and
fde-gate depends on fde-mcp rather than the reverse.

This tool runs as `fde_agent`, which cannot read `wf.agent_launch` (db/018
withholds it from every role but the console's gate role). That is not worked
around here: the launches panel is passed as `None` and both renderers say so
in words. See `fde_mcp.dashboard`'s module docstring for the full privilege
partition and why `None` and `[]` mean different things.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fde_mcp import db
from fde_mcp.dashboard import (
    DRIFT_SQL,
    GATE_KIND_SQL,
    GATES_SQL,
    MERGES_SQL,
    QUEUE_SQL,
    REVIEWERS_SQL,
    RUNS_SQL,
    assemble_dashboard,
    render_dashboard_html,
    render_dashboard_markdown,
)
from fde_mcp.tools._base import emit_trace, fetchall, fetchone, now_ms, pg_error_boundary

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["register"]

# A window this long makes `make_interval(days => ...)` the expensive part of
# nothing at all, but an unbounded one invites a caller to ask for the whole
# history and call it a dashboard. Clamped rather than rejected: a model that
# asks for 3650 days wants "everything", and refusing the call teaches it
# less than quietly giving it the widest window this surface supports and
# saying so in the result.
_MAX_WINDOW_DAYS = 365
_MIN_WINDOW_DAYS = 1


@pg_error_boundary
async def hitl_export_dashboard(
    engagement_id: str | None = None, window_days: int = 30
) -> dict[str, Any]:
    """Export the decision dashboard: how work is flowing through the human gates, as one document.

    Use this when the question is about the SHAPE of the queue rather than
    about one item -- "how are we doing", "what is stuck", "is anything
    overdue", "who has been reviewing", "did anything merge this month",
    "give me something to show the client". Pass `engagement_id` to scope it,
    or omit it for a rollup across every engagement. `window_days` (default
    30) bounds the panels that are about a period -- decisions, merges, runs;
    the queue, gate and drift panels describe the present and ignore it.

    Returns `{markdown, html, generated_at, window_days, engagement_id}`. The
    `markdown` is a whole document -- YAML front matter, headline figures,
    then the review queue, gates (cleared/pending/overdue, median time to
    clear, and a breakdown per gate kind), decisions per reviewer, merges,
    drift and workflow runs. Paste it into a vault, a wiki or a ticket
    unchanged. The `html` is the same numbers as a self-contained styled
    section -- no external stylesheet, script or image -- for embedding or
    emailing.

    WHAT THIS IS NOT: unlike wf_export_playbook, this document is NOT stable
    and NOT pinned to a commit. It reflects live database state at
    `generated_at`, so exporting twice a minute apart across a merge SHOULD
    give different numbers, and a saved copy is stale the moment anything
    happens. Do not diff two exports and treat the difference as a finding,
    and do not cite it as evidence for a proposal: it aggregates counts and
    names no individual proposal, node or edge. When you need something
    citable and reproducible, use kg_as_of or wf_export_playbook instead.

    One panel is missing by design. Agent launches (`wf.agent_launch`) are
    readable only by the console's gate role -- this server runs as
    `fde_agent`, which db/018 deliberately denies -- so the launches section
    says it could not be read. That is NOT a statement that no agents ran;
    the console's /ui/dashboard shows it.

    Read-only: this only counts rows in hitl/kg/sor/wf and cannot propose,
    decide, merge, triage or launch anything.
    """
    t0 = now_ms()
    window = max(_MIN_WINDOW_DAYS, min(int(window_days), _MAX_WINDOW_DAYS))
    now = datetime.now(tz=UTC)
    # `now` is passed into every query rather than each one calling now(), so
    # the whole page describes one instant even though it takes several
    # statements to build. See fde_mcp.dashboard's module docstring.
    params = {"eng": engagement_id, "now": now, "window": window}

    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(QUEUE_SQL, params)
            queue = await fetchall(cur)

            await cur.execute(GATES_SQL, params)
            gates = await fetchone(cur)

            await cur.execute(GATE_KIND_SQL, params)
            gate_kinds = await fetchall(cur)

            await cur.execute(REVIEWERS_SQL, params)
            reviewers = await fetchall(cur)

            await cur.execute(MERGES_SQL, params)
            merges = await fetchone(cur)

            await cur.execute(DRIFT_SQL, params)
            drift = await fetchall(cur)

            await cur.execute(RUNS_SQL, params)
            runs = await fetchall(cur)

        data = assemble_dashboard(
            engagement_id=engagement_id,
            window_days=window,
            now=now,
            queue=queue,
            gates=gates,
            gate_kinds=gate_kinds,
            reviewers=reviewers,
            merges=merges,
            drift=drift,
            runs=runs,
            # Not a privilege this server has, and not a gap to hide: see the
            # module docstring. LAUNCHES_SQL is deliberately not imported.
            launches=None,
        )

        result = {
            "engagement_id": engagement_id,
            "window_days": window,
            "generated_at": data["generated_at"],
            "markdown": render_dashboard_markdown(data),
            "html": render_dashboard_html(data),
        }
        await emit_trace(conn, "hitl_export_dashboard", result, latency_ms=int(now_ms() - t0))
        return result


def register(mcp: FastMCP[None]) -> None:
    """Register the dashboard tool on `mcp`."""
    mcp.tool()(hitl_export_dashboard)
