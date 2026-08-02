"""service/drift.py -- the drift queue and its triage, for product ops.

This is the module that makes docs/10 §4 true. `fde_prodops` has held
SELECT and UPDATE on `sor.drift_signal` since db/010:68, but nothing in the
platform ever offered a human a way to use them -- the only triage path was
the MCP `drift_triage` tool, which runs as `fde_agent` and is forbidden from
marking anything resolved (db/011:29-34).

A person may resolve. That is the difference between the two callers, and
it is why this module does not import the agent-side restriction: a signal
an operator has looked at and judged closed is exactly what 'resolved'
means, and the state is denied to the agent precisely so that it stays a
human's word.
"""

from __future__ import annotations

from typing import Any

from fde_gate.config import get_gate_settings
from fde_gate.rows import fetchall, fetchone
from fde_mcp import db
from fde_mcp.logging import get_logger

log = get_logger(__name__)

__all__ = ["OPEN_STATES", "list_signals", "triage"]

# The states a signal is still someone's problem in, matching
# db/007's drift_signal_queue_idx.
OPEN_STATES: tuple[str, ...] = ("open", "triaged", "proposal_raised")

_MAX_LIMIT = 500


async def list_signals(
    *,
    states: tuple[str, ...] = OPEN_STATES,
    severity: str | None = None,
    engagement_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """The drift queue: worst first, then oldest.

    `sample_size` and `occurrences` are returned next to the severity on
    purpose. db/007's own comment says a signal with n=3 is noise, and a
    queue that shows severity without sample size invites an operator to
    treat those two identically.
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    async with (
        db.tool_transaction(role=get_gate_settings().gate.prodops_role) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            """
            SELECT signal_id, engagement_id, drift_kind::text AS drift_kind,
                   severity::text AS severity, state::text AS state,
                   subject_kind, subject_ref, detail, sample_size, effect_size,
                   detected_at, last_seen_at, occurrences, triaged_by, triaged_at,
                   raised_proposal_id, resolution_note, resolved_at
              FROM sor.drift_signal
             WHERE state = ANY (%(states)s::sor.drift_state[])
               AND (%(severity)s::text IS NULL
                    OR severity = %(severity)s::sor.drift_severity)
               AND (%(eng)s::uuid IS NULL OR engagement_id = %(eng)s::uuid)
             ORDER BY severity DESC, last_seen_at DESC, signal_id
             LIMIT %(limit)s
            """,
            {
                "states": list(states),
                "severity": severity,
                "eng": engagement_id,
                "limit": limit,
            },
        )
        signals = await fetchall(cur)

    return {
        "states": list(states),
        "severity": severity,
        "returned": len(signals),
        "signals": signals,
    }


async def triage(
    signal_id: int,
    principal: str,
    state: str,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Move a drift signal's state and record who moved it.

    `resolved_at` is stamped by the same UPDATE that sets state='resolved',
    rather than by a second statement or a trigger, so a resolved signal can
    never be missing the timestamp that says when -- the pair is written or
    neither is.
    """
    async with (
        db.tool_transaction(role=get_gate_settings().gate.prodops_role) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            """
            UPDATE sor.drift_signal
               SET state           = %(state)s::sor.drift_state,
                   triaged_by      = %(principal)s,
                   triaged_at      = now(),
                   resolution_note = coalesce(%(note)s, resolution_note),
                   resolved_at     = CASE WHEN %(state)s::sor.drift_state = 'resolved'
                                          THEN now() ELSE resolved_at END
             WHERE signal_id = %(sid)s
            RETURNING signal_id, drift_kind::text AS drift_kind,
                      severity::text AS severity, state::text AS state,
                      subject_kind, subject_ref, triaged_by, triaged_at,
                      resolution_note, resolved_at
            """,
            {"sid": signal_id, "principal": principal, "state": state, "note": note},
        )
        signal = await fetchone(cur)

    if signal is None:
        return {"error": f"drift signal {signal_id} not found"}
    log.info("drift_triaged", signal_id=signal_id, principal=principal, state=state)
    return {"signal": signal}
