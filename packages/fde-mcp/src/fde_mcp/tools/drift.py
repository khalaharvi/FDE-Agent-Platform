"""tools/drift.py -- drift monitoring against systems of record.

Detection itself is pure SQL (sor.run_all_detectors and friends in
db/007_drift.sql), not a model judgement -- these tools trigger a run,
list what it found, and let an agent annotate a signal. Only a human can
move a signal to 'resolved' (see `drift_triage`'s docstring); that state
transition is deliberately not reachable through this server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fde_mcp import db
from fde_mcp.config import get_settings
from fde_mcp.tools._base import emit_trace, fetchall, fetchone, now_ms, pg_error_boundary

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["register"]

DRIFT_STATES = ("open", "triaged", "proposal_raised", "accepted", "dismissed", "resolved")
DRIFT_SEVERITIES = ("info", "low", "medium", "high", "critical")


@pg_error_boundary
async def drift_scan(engagement_id: str) -> dict[str, Any]:
    """Run all deterministic drift detectors for this engagement
    (sor.run_all_detectors): sequence drift, control bypass, actor drift,
    latency drift, refreshing the observed-transition materialised view
    first. Detection itself is pure SQL, not a model judgement -- this tool
    only triggers the run and reports how many new/updated signals each
    detector produced. Call drift_list afterwards to see them. This can be
    slow on a large observation table; it is meant for a scheduled monitor
    run, not a tight loop.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT sor.run_all_detectors(%(eng)s::uuid) AS summary", {"eng": engagement_id}
            )
            summary_row = await fetchone(cur)
        summary = summary_row["summary"] if summary_row is not None else None
        result = {"engagement_id": engagement_id, "summary": summary}
        await emit_trace(conn, "drift_scan", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def drift_list(
    engagement_id: str,
    state: Literal[DRIFT_STATES] = "open",  # type: ignore[valid-type]
    min_severity: Literal[DRIFT_SEVERITIES] = "medium",  # type: ignore[valid-type]
) -> dict[str, Any]:
    """List drift signals for this engagement in a given `state`, at or
    above `min_severity` (severity order: info < low < medium < high <
    critical), most severe and most recent first. Each signal's `detail`
    jsonb explains expected vs. observed with sample size/effect size --
    read it before triaging. Use drift_triage to annotate a signal; only a
    human can move a signal to 'resolved'.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT signal_id, drift_kind, severity, state, subject_kind, subject_ref,
                       detail, sample_size, effect_size, detected_at, last_seen_at,
                       occurrences, triaged_by, triaged_at, raised_proposal_id
                  FROM sor.drift_signal
                 WHERE engagement_id = %(eng)s::uuid
                   AND state = %(state)s::sor.drift_state
                   AND severity >= %(min_sev)s::sor.drift_severity
                 ORDER BY severity DESC, detected_at DESC
                """,
                {"eng": engagement_id, "state": state, "min_sev": min_severity},
            )
            rows = await fetchall(cur)
        result = {
            "engagement_id": engagement_id,
            "state": state,
            "min_severity": min_severity,
            "returned": len(rows),
            "signals": rows,
        }
        await emit_trace(conn, "drift_list", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def drift_triage(
    engagement_id: str,
    signal_id: int,
    state: Literal["open", "triaged", "proposal_raised", "accepted", "dismissed"],
    note: str,
) -> dict[str, Any]:
    """Annotate a drift signal: move it to `state` and record `note`.

    `state='resolved'` is INTENTIONALLY NOT AN ACCEPTED VALUE HERE -- only a
    human reviewer can resolve a drift signal (this tool will reject the
    call before it reaches the database if you try). Typical flow: 'open'
    -> 'triaged' (you've read it and formed a view) -> 'proposal_raised'
    (you called kg_propose to fix it and want that linked -- set
    raised_proposal_id by re-triaging is not supported here; link it via the
    proposal's own rationale instead) or 'dismissed' (noise, e.g. n too
    small) or 'accepted' (real, but no graph change proposed, e.g. it is
    tracked in the workflow instead).
    """
    t0 = now_ms()
    triaged_by = get_settings().agent.principal
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE sor.drift_signal
                   SET state = %(state)s::sor.drift_state,
                       triaged_by = %(triaged_by)s,
                       triaged_at = now(),
                       resolution_note = %(note)s
                 WHERE engagement_id = %(eng)s::uuid AND signal_id = %(sid)s
                 RETURNING signal_id, drift_kind, severity, state, triaged_by, triaged_at, resolution_note
                """,
                {
                    "eng": engagement_id,
                    "sid": signal_id,
                    "state": state,
                    "triaged_by": triaged_by,
                    "note": note,
                },
            )
            row = await fetchone(cur)
        result: dict[str, Any]
        if row is None:
            result = {
                "error": "drift signal not found for this engagement",
                "hint": "check engagement_id and signal_id",
            }
        else:
            result = {"signal": row}
        await emit_trace(conn, "drift_triage", result, latency_ms=int(now_ms() - t0))
        return result


def register(mcp: FastMCP[None]) -> None:
    """Register every drift tool on `mcp`."""
    mcp.tool()(drift_scan)
    mcp.tool()(drift_list)
    mcp.tool()(drift_triage)
