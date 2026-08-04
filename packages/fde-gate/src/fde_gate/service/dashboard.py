"""service/dashboard.py -- the aggregate decision view, assembled from two roles.

Every other module in this package runs as ONE database role, and the package
docstring uses that to make the privilege boundary visible in the import
graph. This module is the exception, and it is an exception the schema forces
rather than one chosen for convenience:

    panel                     table                 role that may read it
    queue / gates / merges    hitl.proposal, ...    both
    drift                     sor.drift_signal      fde_prodops only
    workflow runs             wf.run                fde_prodops only
    decisions by reviewer     hitl.reviewer         fde_gate_service only
    agent launches            wf.agent_launch       fde_gate_service only

`hitl.gate_decision.reviewer_id` is a foreign key to `hitl.reviewer`, not a
principal string, so naming the reviewer who approved something needs a join
`fde_prodops` cannot perform; and db/018 grants `wf.agent_launch` to the gate
service alone. No single role can render this page. `/ui/runs` already
resolves the same tension the same way -- runs through prodops, launches
through the gate role, two read-only transactions on one page -- and this
follows it rather than inventing a role that could see everything, which is
what db/010 exists to prevent.

The queries and both renderers live in `fde_mcp.dashboard`, beside
`fde_mcp.playbook`, so this page and the `hitl_export_dashboard` MCP tool
cannot disagree about a number. Only the role selection is decided here.

No reviewer check, deliberately, matching `runs.list_runs`,
`drift.list_signals` and `agents.list_launches`: this is aggregate counts
over work the console already shows every authenticated principal in detail,
and gating it would hide the summary from people who can read every row it
summarises.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fde_gate.config import get_gate_settings
from fde_mcp import db
from fde_mcp.dashboard import (
    DRIFT_SQL,
    GATE_KIND_SQL,
    GATES_SQL,
    LAUNCHES_SQL,
    MERGES_SQL,
    QUEUE_SQL,
    REVIEWERS_SQL,
    RUNS_SQL,
    assemble_dashboard,
    render_dashboard_html,
    render_dashboard_markdown,
)

__all__ = ["MAX_WINDOW_DAYS", "WINDOW_CHOICES", "build"]

#: The windows the console offers. A select rather than a free number field:
#: the value only ever bounds a few COUNT(*)s, but an operator typing 100000
#: into a URL should get a page, not a surprise, and a fixed list makes the
#: clamp below unreachable from the UI rather than merely survivable.
WINDOW_CHOICES: tuple[int, ...] = (7, 30, 90, 365)
MAX_WINDOW_DAYS = 365
_MIN_WINDOW_DAYS = 1

_ENGAGEMENTS_SQL = """
    SELECT DISTINCT engagement_id FROM (
      SELECT engagement_id FROM kg.commit
      UNION SELECT engagement_id FROM kg.source
      UNION SELECT engagement_id FROM hitl.proposal
    ) e ORDER BY engagement_id
"""


def _prodops_role() -> str:
    return get_gate_settings().gate.prodops_role


def _gate_role() -> str:
    return get_gate_settings().gate.gate_role


async def build(*, engagement_id: str | None = None, window_days: int = 30) -> dict[str, Any]:
    """Assemble the dashboard for the console and `/api/dashboard.md`.

    Returns `{data, markdown, html, engagements, engagement_id, window_days,
    generated_at}` -- the assembled dict plus both rendered forms, because
    the page needs the HTML and the .md route needs the Markdown and running
    the queries twice to get them would let the two disagree.

    Both panels the gate role owns are read in a second transaction. That
    transaction is opened unconditionally rather than lazily: the page always
    shows both sections, and a page whose contents depend on whether an
    earlier query happened to return rows is harder to reason about than one
    extra read-only statement.
    """
    window = max(_MIN_WINDOW_DAYS, min(int(window_days), MAX_WINDOW_DAYS))
    # One instant for both transactions. Two calls to now() would put the
    # queue and the reviewer table minutes apart on a slow day, which is the
    # kind of inconsistency nobody notices and everybody eventually trusts.
    now = datetime.now(tz=UTC)
    params = {"eng": engagement_id, "now": now, "window": window}

    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        # The picker's options, same union `sources._known_engagements` uses:
        # there is no engagement table, so "every engagement" is whatever has
        # left a commit, a source or a proposal behind.
        await cur.execute(_ENGAGEMENTS_SQL)
        engagements = [str(row["engagement_id"]) for row in await cur.fetchall()]

        await cur.execute(QUEUE_SQL, params)
        queue = list(await cur.fetchall())
        await cur.execute(GATES_SQL, params)
        gates = await cur.fetchone()
        await cur.execute(GATE_KIND_SQL, params)
        gate_kinds = list(await cur.fetchall())
        await cur.execute(MERGES_SQL, params)
        merges = await cur.fetchone()
        await cur.execute(DRIFT_SQL, params)
        drift = list(await cur.fetchall())
        await cur.execute(RUNS_SQL, params)
        runs = list(await cur.fetchall())

    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute(REVIEWERS_SQL, params)
        reviewers = list(await cur.fetchall())
        await cur.execute(LAUNCHES_SQL, params)
        launches = list(await cur.fetchall())

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
        launches=launches,
    )
    return {
        "engagement_id": engagement_id,
        "window_days": window,
        "generated_at": data["generated_at"],
        "engagements": engagements,
        "data": data,
        "markdown": render_dashboard_markdown(data),
        # `titled=False`: dashboard.html.j2 already gives the page its own
        # <h1>, the way every console page does, and the section repeating it
        # a few lines below reads as a template bug.
        "html": render_dashboard_html(data, titled=False),
    }
