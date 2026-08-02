"""service/proposals.py -- the review queue, the review page, and the merge.

Runs as `fde_gate_service` throughout. That role holds SELECT/INSERT/UPDATE
on every hitl table (db/010:48), SELECT on kg.* for the evidence join, and
EXECUTE on the five transitions this module calls: `hitl.record_decision`,
`hitl.edit_item`, `hitl.merge_proposal`, `hitl.apply_trace_label` and
`hitl.expire_proposals`. No other role in the platform holds the last three,
which is the whole point of the split.

Two things this module goes out of its way to surface, because docs/10 §6
says an operator's real questions are "which gate is still open?" and "who
can clear it?":

* Every gate carries its live approval count, whether a live reject or
  changes-requested is blocking it, and whether the CALLING principal is one
  of the people who could clear it.
* The proposal detail carries the roster of reviewers who still hold live
  authority on each open gate. A queue that shows a blocked proposal without
  naming who can unblock it is a queue that generates a Slack message.

`hitl.gate_quorum_met` is deliberately NOT called from here even though the
gate role holds EXECUTE on it. It is an internal helper that
`hitl.record_decision` uses to stamp `proposal_gate.cleared_at`, and
`cleared_at` is therefore already the durable, transactional answer to "has
this gate been met" -- reading the predicate again from a display query
would be a second source for a question the row already answers.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from fde_gate.config import get_gate_settings
from fde_gate.http import as_conflict
from fde_gate.rows import fetchall, fetchone
from fde_mcp import db
from fde_mcp.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "OPEN_STATUSES",
    "edit_item",
    "expire",
    "get_proposal",
    "list_queue",
    "merge",
    "record_decision",
]

# The three non-terminal proposal states, matching db/004's proposal_queue_idx
# and db/013's hitl.expire_proposals exactly. A queue default that drifted
# from the partial index would quietly stop using it.
OPEN_STATUSES: tuple[str, ...] = ("submitted", "in_review", "changes_requested")

_MAX_LIMIT = 500


def _gate_role() -> str:
    return get_gate_settings().gate.gate_role


# The live-approval count and the "could this principal clear it" predicate,
# both spelled the same way hitl.gates_satisfied spells them: distinct,
# currently-authorised, active, non-self reviewers. Duplicated from SQL
# rather than approximated -- a queue that shows a gate as clearable by
# someone hitl.record_decision will then refuse is worse than showing
# nothing.
_GATES_SQL = """
SELECT g.gate_id, g.proposal_id, g.gate_kind::text AS gate_kind, g.quorum,
       g.allow_self, g.triggering_items, g.due_at, g.cleared_at,
       (SELECT count(DISTINCT d.reviewer_id)
          FROM hitl.gate_decision d
          JOIN hitl.reviewer r ON r.reviewer_id = d.reviewer_id
          JOIN hitl.reviewer_authority ra
            ON ra.reviewer_id   = d.reviewer_id
           AND ra.gate_kind     = g.gate_kind
           AND ra.revoked_at   IS NULL
           AND ra.engagement_id = p.engagement_id
         WHERE d.gate_id = g.gate_id
           AND d.decision = 'approve'
           AND d.superseded_by IS NULL
           AND r.is_active
           AND (g.allow_self OR r.principal <> p.authored_by)) AS approvals,
       EXISTS (SELECT 1 FROM hitl.gate_decision d
                WHERE d.gate_id = g.gate_id AND d.superseded_by IS NULL
                  AND d.decision IN ('reject','request_changes')) AS blocked,
       EXISTS (SELECT 1
                 FROM hitl.reviewer_authority ra
                 JOIN hitl.reviewer r ON r.reviewer_id = ra.reviewer_id
                WHERE ra.engagement_id = p.engagement_id
                  AND ra.gate_kind     = g.gate_kind
                  AND ra.revoked_at   IS NULL
                  AND r.is_active
                  AND r.principal      = %(me)s
                  AND (g.allow_self OR r.principal <> p.authored_by)) AS i_can_clear,
       g.due_at < now() AS overdue
  FROM hitl.proposal_gate g
  JOIN hitl.proposal p ON p.proposal_id = g.proposal_id
 WHERE g.proposal_id = ANY(%(ids)s)
 ORDER BY g.due_at, g.gate_id
"""


async def list_queue(
    principal: str,
    *,
    statuses: tuple[str, ...] = OPEN_STATUSES,
    engagement_id: str | None = None,
    mine: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Proposals awaiting review, each with its gates.

    `mine=True` narrows to proposals with at least one OPEN gate this
    principal is authorised to clear -- the operator's actual working set,
    as opposed to everything in flight.
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.proposal_id, p.proposal_uuid, p.engagement_id, p.title, p.rationale,
                   p.status::text AS status, p.agent_name, p.authored_by, p.model_id,
                   p.trace_session_id, p.submitted_at, p.expires_at, p.decided_at,
                   p.merged_commit_id, p.created_at,
                   p.expires_at < now() AS expired,
                   (SELECT count(*) FROM hitl.proposal_item i
                     WHERE i.proposal_id = p.proposal_id) AS item_count
              FROM hitl.proposal p
             WHERE p.status = ANY(%(statuses)s::hitl.proposal_status[])
               AND (%(eng)s::uuid IS NULL OR p.engagement_id = %(eng)s::uuid)
             ORDER BY p.submitted_at NULLS LAST, p.proposal_id
             LIMIT %(limit)s
            """,
            {"statuses": list(statuses), "eng": engagement_id, "limit": limit},
        )
        proposals = await fetchall(cur)

        ids = [row["proposal_id"] for row in proposals]
        gates: list[dict[str, Any]] = []
        if ids:
            await cur.execute(_GATES_SQL, {"ids": ids, "me": principal})
            gates = await fetchall(cur)

    by_proposal: dict[int, list[dict[str, Any]]] = {}
    for gate in gates:
        by_proposal.setdefault(int(gate["proposal_id"]), []).append(gate)
    for row in proposals:
        row["gates"] = by_proposal.get(int(row["proposal_id"]), [])
        row["open_gates"] = [g for g in row["gates"] if g["cleared_at"] is None]

    if mine:
        proposals = [
            row for row in proposals if any(gate["i_can_clear"] for gate in row["open_gates"])
        ]

    return {
        "principal": principal,
        "statuses": list(statuses),
        "engagement_id": engagement_id,
        "mine": mine,
        "returned": len(proposals),
        "proposals": proposals,
    }


async def get_proposal(proposal_id: int, principal: str) -> dict[str, Any]:
    """One proposal in full: items with their evidence, gates with their
    decisions, and the roster still authorised to clear each open gate.

    `original_payload` is returned alongside `payload` on every item so the
    console can render the agent's version beside the reviewer's edit --
    docs/07 calls that delta the most valuable event in the system, and it is
    only legible if both halves reach the page.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.proposal_id, p.proposal_uuid, p.engagement_id, p.title, p.rationale,
                   p.status::text AS status, p.agent_name, p.authored_by, p.model_id,
                   p.base_commit_id, p.trace_session_id, p.merged_commit_id,
                   p.submitted_at, p.decided_at, p.expires_at, p.created_at, p.updated_at,
                   hitl.gates_satisfied(p.proposal_id) AS gates_satisfied
              FROM hitl.proposal p
             WHERE p.proposal_id = %(pid)s
            """,
            {"pid": proposal_id},
        )
        proposal = await fetchone(cur)
        if proposal is None:
            return {"error": f"proposal {proposal_id} not found"}

        await cur.execute(
            """
            SELECT item_id, ordinal, op, node_type::text AS node_type,
                   edge_type::text AS edge_type, subject_key, payload, original_payload,
                   supersedes_key, source_ids, agent_confidence, item_status,
                   edited_by, edited_at
              FROM hitl.proposal_item
             WHERE proposal_id = %(pid)s
             ORDER BY ordinal
            """,
            {"pid": proposal_id},
        )
        items = await fetchall(cur)

        # Evidence, joined through the item's source_ids array. docs/10 §3
        # requires the reviewer to see what each claim is grounded in without
        # leaving the page.
        await cur.execute(
            """
            SELECT i.item_id, s.source_id, s.source_kind, s.title, s.uri,
                   s.captured_at, s.captured_by
              FROM hitl.proposal_item i
              JOIN kg.source s ON s.source_id = ANY (i.source_ids)
             WHERE i.proposal_id = %(pid)s
             ORDER BY i.item_id, s.source_id
            """,
            {"pid": proposal_id},
        )
        evidence = await fetchall(cur)

        await cur.execute(_GATES_SQL, {"ids": [proposal_id], "me": principal})
        gates = await fetchall(cur)

        await cur.execute(
            """
            SELECT d.decision_id, d.gate_id, d.decision::text AS decision, d.comment,
                   d.item_verdicts, d.review_seconds, d.decided_at, d.superseded_by,
                   r.principal, r.display_name
              FROM hitl.gate_decision d
              JOIN hitl.reviewer r ON r.reviewer_id = d.reviewer_id
              JOIN hitl.proposal_gate g ON g.gate_id = d.gate_id
             WHERE g.proposal_id = %(pid)s
             ORDER BY d.decided_at, d.decision_id
            """,
            {"pid": proposal_id},
        )
        decisions = await fetchall(cur)

        # Who is still authorised on each gate kind, minus anyone whose live
        # approve is already counted. This is docs/10 §6's "who is authorised"
        # question answered as a list of names rather than a policy reference.
        await cur.execute(
            """
            SELECT g.gate_id, r.principal, r.display_name,
                   EXISTS (SELECT 1 FROM hitl.gate_decision d
                            WHERE d.gate_id = g.gate_id AND d.reviewer_id = r.reviewer_id
                              AND d.superseded_by IS NULL) AS has_decided
              FROM hitl.proposal_gate g
              JOIN hitl.proposal p ON p.proposal_id = g.proposal_id
              JOIN hitl.reviewer_authority ra
                ON ra.engagement_id = p.engagement_id
               AND ra.gate_kind     = g.gate_kind
               AND ra.revoked_at   IS NULL
              JOIN hitl.reviewer r ON r.reviewer_id = ra.reviewer_id AND r.is_active
             WHERE g.proposal_id = %(pid)s
               AND (g.allow_self OR r.principal <> p.authored_by)
             ORDER BY g.gate_id, r.principal
            """,
            {"pid": proposal_id},
        )
        roster = await fetchall(cur)

    evidence_by_item: dict[int, list[dict[str, Any]]] = {}
    for row in evidence:
        evidence_by_item.setdefault(int(row["item_id"]), []).append(row)
    for item in items:
        item["evidence"] = evidence_by_item.get(int(item["item_id"]), [])

    roster_by_gate: dict[int, list[dict[str, Any]]] = {}
    for row in roster:
        roster_by_gate.setdefault(int(row["gate_id"]), []).append(row)
    decisions_by_gate: dict[int, list[dict[str, Any]]] = {}
    for row in decisions:
        decisions_by_gate.setdefault(int(row["gate_id"]), []).append(row)
    for gate in gates:
        gate_id = int(gate["gate_id"])
        gate["authorized"] = roster_by_gate.get(gate_id, [])
        gate["decisions"] = decisions_by_gate.get(gate_id, [])
        gate["live_decisions"] = [d for d in gate["decisions"] if d["superseded_by"] is None]

    proposal["items"] = items
    proposal["gates"] = gates
    proposal["principal"] = principal
    return proposal


async def record_decision(
    gate_id: int,
    principal: str,
    decision: str,
    *,
    comment: str | None = None,
    item_verdicts: dict[str, Any] | None = None,
    review_seconds: int | None = None,
) -> dict[str, Any]:
    """Record a review decision through `hitl.record_decision`.

    Every reason the decision would not have counted -- unknown principal,
    inactive reviewer, no authority for this gate kind, self-review on a gate
    that forbids it, proposal already closed -- comes back as a plpgsql RAISE
    and reaches the caller verbatim as a 400. That is deliberate: db/013's
    header explains that `gates_satisfied` has to be silent about these
    because it is a predicate, so this is the only layer at which a reviewer
    finds out their click did nothing.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT * FROM hitl.record_decision(
                %(gate)s, %(principal)s, %(decision)s::hitl.decision,
                %(comment)s, %(verdicts)s::jsonb, %(seconds)s)
            """,
            {
                "gate": gate_id,
                "principal": principal,
                "decision": decision,
                "comment": comment,
                "verdicts": Jsonb(item_verdicts or {}),
                "seconds": review_seconds,
            },
        )
        recorded = await fetchone(cur)

        await cur.execute(
            """
            SELECT p.proposal_id, p.status::text AS status, p.decided_at,
                   hitl.gates_satisfied(p.proposal_id) AS gates_satisfied
              FROM hitl.proposal p
              JOIN hitl.proposal_gate g ON g.proposal_id = p.proposal_id
             WHERE g.gate_id = %(gate)s
            """,
            {"gate": gate_id},
        )
        proposal = await fetchone(cur)

    log.info(
        "gate_decision_recorded",
        gate_id=gate_id,
        principal=principal,
        decision=decision,
        review_seconds=review_seconds,
        proposal_status=None if proposal is None else proposal["status"],
    )
    return {"decision": recorded, "proposal": proposal}


async def edit_item(item_id: int, principal: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Rewrite one item's payload, preserving the agent's original.

    `hitl.edit_item` writes `original_payload` exactly once and never
    overwrites it, so a second edit still shows the delta against what the
    agent proposed rather than against the previous human's version.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT * FROM hitl.edit_item(%(item)s, %(principal)s, %(payload)s::jsonb)",
            {"item": item_id, "principal": principal, "payload": Jsonb(payload)},
        )
        item = await fetchone(cur)
    log.info("proposal_item_edited", item_id=item_id, principal=principal)
    return {"item": item}


async def merge(proposal_id: int, principal: str) -> dict[str, Any]:
    """Merge an approved proposal and label its trace, in ONE transaction.

    The two must not be separable. `hitl.merge_proposal` re-checks the gate
    quorum inside its own transaction and seals a commit; `apply_trace_label`
    reads the resulting `merged` status to decide between 'accepted' and
    'corrected'. Splitting them across transactions would leave a window in
    which the graph has the change and the training set has no label for the
    session that produced it -- which is not a crash, just a permanently
    unlabelled trace nobody would ever notice.

    A proposal that is not yet approved fails here as a 409 rather than a
    400: nothing about the REQUEST is wrong, the resource is simply not in a
    state that permits it.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        with as_conflict():
            await cur.execute(
                "SELECT * FROM hitl.merge_proposal(%(pid)s, %(principal)s)",
                {"pid": proposal_id, "principal": principal},
            )
            commit = await fetchone(cur)
            await cur.execute("SELECT hitl.apply_trace_label(%(pid)s)", {"pid": proposal_id})

        await cur.execute(
            """
            SELECT p.proposal_id, p.status::text AS status, p.merged_commit_id,
                   p.trace_session_id, t.outcome::text AS trace_outcome,
                   t.label_source
              FROM hitl.proposal p
              LEFT JOIN trn.trace_session t ON t.session_id = p.trace_session_id
             WHERE p.proposal_id = %(pid)s
            """,
            {"pid": proposal_id},
        )
        proposal = await fetchone(cur)

    log.info(
        "proposal_merged",
        proposal_id=proposal_id,
        principal=principal,
        commit_id=None if commit is None else commit.get("commit_id"),
        trace_outcome=None if proposal is None else proposal.get("trace_outcome"),
    )
    return {"commit": commit, "proposal": proposal}


async def expire() -> dict[str, Any]:
    """Expire proposals past their SLA. The hourly EventBridge sweep.

    Deliberately does not label the traces it expires: db/013 explains that
    an expiry is an SLA breach, not a human judgment, so the outcome stays
    'pending' and the session never reaches `trn.sft_export`.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute("SELECT hitl.expire_proposals() AS expired")
        row = await fetchone(cur)
    expired = 0 if row is None else int(row["expired"])
    log.info("proposals_expired", expired=expired)
    return {"expired": expired}
