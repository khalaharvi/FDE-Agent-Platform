"""tools/proposals.py -- human-in-the-loop change-proposal tools.

Agents cannot write kg.node/kg.edge under any circumstances (REVOKEd
explicitly in db/004_hitl_gates.sql and db/010_roles_and_seed_policy.sql).
The only way graph facts change is `hitl.propose -> hitl.submit_proposal ->
(human gate) -> hitl.merge_proposal`, and `merge_proposal` is not even
reachable from the `fde_agent` role these tools run as. `kg_propose` stages
a draft; `kg_submit_proposal` freezes the required reviewers and starts
their SLA clocks; a human's approval (outside this server entirely) is what
actually merges a proposal into the graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from fde_mcp import db
from fde_mcp.config import get_settings
from fde_mcp.tools._base import (
    EdgeType,
    NodeType,
    emit_trace,
    fetchall,
    fetchone,
    now_ms,
    pg_error_boundary,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["ProposalItem", "register"]

PROPOSAL_OPS = (
    "add_node",
    "update_node",
    "retire_node",
    "add_edge",
    "update_edge",
    "retire_edge",
)


class ProposalItem(BaseModel):
    """One mutation inside a `kg_propose` call. Mirrors hitl.proposal_item.

    Exactly one of node_type/edge_type must be set, matching op:
    add/update/retire_node -> node_type set, edge_type null;
    add/update/retire_edge -> edge_type set, node_type null. The database
    enforces this (item_type_matches_op); a mismatch is rejected with the
    check-violation message surfaced verbatim.
    """

    op: Literal[PROPOSAL_OPS]  # type: ignore[valid-type]
    node_type: NodeType | None = None
    edge_type: EdgeType | None = None
    subject_key: str = Field(description="node_key or edge_key being created/changed")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            'Full desired end state, e.g. {"label":..., "summary":..., '
            '"attributes":{...}} for a node, or {"src_key":..., '
            '"dst_key":..., "label":..., "attributes":{...}, '
            '"weight":...} for an edge. Ignored for retire_*.'
        ),
    )
    supersedes_key: str | None = Field(
        default=None, description="node_key/edge_key this item replaces, if any"
    )
    source_ids: list[int] = Field(
        default_factory=list,
        description=(
            "kg.source.source_id values backing this assertion. REQUIRED "
            "(non-empty) for every add_*/update_* item before the proposal "
            "can be submitted -- hitl.submit_proposal rejects unsourced items."
        ),
    )
    agent_confidence: float = Field(ge=0.0, le=1.0)


@pg_error_boundary
async def kg_propose(
    engagement_id: str,
    *,
    title: str,
    rationale: str,
    items: list[ProposalItem],
    base_commit_id: int,
    trace_session_id: str | None = None,
) -> dict[str, Any]:
    """Stage a proposed change to the graph. DOES NOT WRITE kg.node/kg.edge
    and DOES NOT SUBMIT -- it only creates a hitl.proposal in 'draft' status
    plus its hitl.proposal_item rows. Call kg_submit_proposal separately
    once you are done adding/editing items.

    `base_commit_id` should be the commit_id from kg_head_commit (or the
    commit you last reasoned over) -- it records what graph state this
    proposal was authored against, for reproducible review.

    Returns `proposal_id` and `required_gates`: the LIVE (not yet frozen)
    output of hitl.compute_required_gates for the items as given right now.
    Use this to tell the human up front which reviewer types (ontology /
    factual / control / automation) and how many of each will be needed --
    it will change if you add/remove items before submitting, and is
    re-computed and FROZEN at submit time by kg_submit_proposal.

    Every add_*/update_* item needs >= 1 source_ids entry or submission will
    fail (see kg_submit_proposal's docstring).
    """
    t0 = now_ms()
    agent = get_settings().agent
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO hitl.proposal
                    (engagement_id, title, rationale, authored_by, agent_name,
                     model_id, base_commit_id, trace_session_id)
                VALUES (%(eng)s::uuid, %(title)s, %(rationale)s, %(authored_by)s,
                        %(agent_name)s, %(model_id)s, %(base_commit_id)s,
                        %(trace_session_id)s::uuid)
                RETURNING proposal_id, proposal_uuid, status, created_at
                """,
                {
                    "eng": engagement_id,
                    "title": title,
                    "rationale": rationale,
                    "authored_by": agent.runtime_arn,
                    "agent_name": agent.name,
                    "model_id": agent.model_id,
                    "base_commit_id": base_commit_id,
                    "trace_session_id": trace_session_id or agent.trace_session_id,
                },
            )
            proposal = await fetchone(cur)
            assert proposal is not None, "RETURNING always yields exactly one row here"
            proposal_id = proposal["proposal_id"]

            for ordinal, item in enumerate(items, start=1):
                await cur.execute(
                    """
                    INSERT INTO hitl.proposal_item
                        (proposal_id, ordinal, op, node_type, edge_type, subject_key,
                         payload, supersedes_key, source_ids, agent_confidence)
                    VALUES (%(pid)s, %(ordinal)s, %(op)s, %(node_type)s::kg.node_type,
                            %(edge_type)s::kg.edge_type, %(subject_key)s,
                            %(payload)s, %(supersedes_key)s, %(source_ids)s,
                            %(agent_confidence)s)
                    """,
                    {
                        "pid": proposal_id,
                        "ordinal": ordinal,
                        "op": item.op,
                        "node_type": item.node_type,
                        "edge_type": item.edge_type,
                        "subject_key": item.subject_key,
                        "payload": Jsonb(item.payload),
                        "supersedes_key": item.supersedes_key,
                        "source_ids": item.source_ids,
                        "agent_confidence": item.agent_confidence,
                    },
                )

            await cur.execute(
                "SELECT policy_id, gate_kind, quorum, allow_self, sla_hours, triggering_items "
                "FROM hitl.compute_required_gates(%(pid)s)",
                {"pid": proposal_id},
            )
            gates = await fetchall(cur)

        result = {
            "proposal_id": proposal_id,
            "proposal_uuid": str(proposal["proposal_uuid"]),
            "status": proposal["status"],
            "item_count": len(items),
            "required_gates": gates,
            "note": (
                "draft only -- nothing merged, nothing submitted. Call "
                "kg_submit_proposal(proposal_id) when ready; required_gates "
                "above is a preview and will be recomputed and frozen at "
                "submit time."
            ),
        }
        await emit_trace(conn, "kg_propose", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def kg_submit_proposal(proposal_id: int) -> dict[str, Any]:
    """Submit a draft/changes-requested proposal for human review
    (hitl.submit_proposal).

    Validates that every non-retire item has >= 1 evidence source, computes
    and FREEZES the required gate set (hitl.proposal_gate rows with due
    dates, one SLA clock per gate), and moves the proposal to 'submitted'.

    If validation fails -- no items, or any item missing evidence, or (fail
    closed) no gate policy matched at all -- the database RAISEs and this
    tool surfaces that message VERBATIM so you can fix the proposal (usually
    by calling kg_propose again with source_ids populated, or adding an
    item) and resubmit. This never partially submits.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM hitl.submit_proposal(%(pid)s)", {"pid": proposal_id})
            proposal = await fetchone(cur)

            await cur.execute(
                """
                SELECT gate_id, gate_kind, quorum, allow_self, triggering_items, due_at
                  FROM hitl.proposal_gate
                 WHERE proposal_id = %(pid)s
                 ORDER BY due_at
                """,
                {"pid": proposal_id},
            )
            gates = await fetchall(cur)

        result = {"proposal": proposal, "gates": gates}
        await emit_trace(conn, "kg_submit_proposal", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def kg_proposal_status(proposal_id: int) -> dict[str, Any]:
    """Status of a submitted proposal: which gates are cleared, who has
    decided so far, and which authorised reviewers are still needed.

    For each frozen gate, reports quorum, how many DISTINCT authorised
    non-superseded 'approve' decisions it has (respecting allow_self and
    reviewer.is_active), the raw decision list, and the principals still
    eligible and outstanding to clear it. `overall_satisfied` mirrors
    hitl.gates_satisfied -- true only once every gate has quorum and none
    has a live reject/request_changes.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM hitl.proposal WHERE proposal_id = %(pid)s", {"pid": proposal_id}
            )
            proposal = await fetchone(cur)
            if proposal is None:
                result: dict[str, Any] = {
                    "error": "proposal not found",
                    "hint": "check proposal_id",
                }
                await emit_trace(conn, "kg_proposal_status", result, latency_ms=int(now_ms() - t0))
                return result

            await cur.execute(
                "SELECT gate_id, gate_kind, quorum, allow_self, triggering_items, due_at, cleared_at "
                "FROM hitl.proposal_gate WHERE proposal_id = %(pid)s ORDER BY due_at",
                {"pid": proposal_id},
            )
            gates = await fetchall(cur)

            gate_summaries = []
            for gate in gates:
                await cur.execute(
                    """
                    SELECT d.decision_id, r.principal, r.display_name, d.decision,
                           d.comment, d.decided_at
                      FROM hitl.gate_decision d
                      JOIN hitl.reviewer r ON r.reviewer_id = d.reviewer_id
                     WHERE d.gate_id = %(gid)s AND d.superseded_by IS NULL
                     ORDER BY d.decided_at
                    """,
                    {"gid": gate["gate_id"]},
                )
                decisions = await fetchall(cur)

                approving = [
                    d["principal"]
                    for d in decisions
                    if d["decision"] == "approve"
                    and (gate["allow_self"] or d["principal"] != proposal["authored_by"])
                ]
                blocking = [d for d in decisions if d["decision"] in ("reject", "request_changes")]

                await cur.execute(
                    """
                    SELECT r.principal, r.display_name
                      FROM hitl.reviewer_authority ra
                      JOIN hitl.reviewer r ON r.reviewer_id = ra.reviewer_id
                     WHERE ra.engagement_id = %(eng)s::uuid AND ra.gate_kind = %(kind)s::hitl.gate_kind
                       AND ra.revoked_at IS NULL AND r.is_active
                    """,
                    {"eng": proposal["engagement_id"], "kind": gate["gate_kind"]},
                )
                eligible = await fetchall(cur)
                still_needed = [r for r in eligible if r["principal"] not in approving]

                gate_summaries.append(
                    {
                        **gate,
                        "distinct_approvals": len(set(approving)),
                        "quorum_met": len(set(approving)) >= gate["quorum"],
                        "has_blocking_decision": bool(blocking),
                        "decisions": decisions,
                        "still_needed_reviewers": still_needed
                        if len(set(approving)) < gate["quorum"]
                        else [],
                    }
                )

            await cur.execute(
                "SELECT hitl.gates_satisfied(%(pid)s) AS satisfied", {"pid": proposal_id}
            )
            overall_row = await fetchone(cur)
            overall = overall_row["satisfied"] if overall_row is not None else False

        result = {"proposal": proposal, "gates": gate_summaries, "overall_satisfied": overall}
        await emit_trace(conn, "kg_proposal_status", result, latency_ms=int(now_ms() - t0))
        return result


def register(mcp: FastMCP[None]) -> None:
    """Register every proposal tool on `mcp`."""
    mcp.tool()(kg_propose)
    mcp.tool()(kg_submit_proposal)
    mcp.tool()(kg_proposal_status)
