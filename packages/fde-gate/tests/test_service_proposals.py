"""The review flow, end to end, against live Postgres.

These exercise the boundary the whole platform exists to hold: an agent
proposes, a human disposes, and the human's edit is what reaches the graph
and the training set.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any

import pytest
from gate_seed import AGENT_PRINCIPAL, COMPLIANCE, OWNER, SME, STRANGER
from psycopg import errors as pg_errors

from fde_gate.handler import post_decision
from fde_gate.http import GateError, Request, error_response
from fde_gate.service import proposals

pytestmark = pytest.mark.requires_db


def _gate(created: dict[str, Any], kind: str) -> int:
    return next(g["gate_id"] for g in created["gates"] if g["gate_kind"] == kind)


async def _clear_all_gates(created: dict[str, Any], principal: str = SME) -> None:
    """Approve every gate on a proposal, which is what reaching quorum means.

    A decision is recorded against ONE gate, so a proposal with an ontology
    gate and a factual gate needs two approvals even at quorum 1 each.
    """
    for gate in created["gates"]:
        await proposals.record_decision(gate["gate_id"], principal, "approve")


async def test_queue_lists_open_gates_for_my_principal(make_proposal: Any) -> None:
    created = make_proposal(title="queue fixture proposal")

    queue = await proposals.list_queue(SME)
    mine = next(p for p in queue["proposals"] if p["proposal_id"] == created["proposal_id"])
    assert mine["status"] == "submitted"
    assert mine["item_count"] == 1
    kinds = {g["gate_kind"] for g in mine["open_gates"]}
    assert kinds == {"ontology", "factual"}
    # SME holds ontology AND factual authority on this engagement.
    assert all(g["i_can_clear"] for g in mine["open_gates"])
    assert all(g["approvals"] == 0 for g in mine["open_gates"])

    # COMPLIANCE holds ontology and control, so the factual gate is not theirs.
    compliance_view = await proposals.list_queue(COMPLIANCE)
    theirs = next(
        p for p in compliance_view["proposals"] if p["proposal_id"] == created["proposal_id"]
    )
    clearable = {g["gate_kind"] for g in theirs["open_gates"] if g["i_can_clear"]}
    assert clearable == {"ontology"}

    # A principal with no authority at all sees the queue but can clear none
    # of it -- and `mine=1` is then empty rather than misleading.
    stranger_view = await proposals.list_queue(STRANGER, mine=True)
    assert not [p for p in stranger_view["proposals"] if p["proposal_id"] == created["proposal_id"]]


async def test_detail_includes_items_evidence_and_gate_roster(
    make_proposal: Any, seed: dict[str, Any]
) -> None:
    created = make_proposal(
        items=[
            {
                "op": "add_node",
                "node_type": "activity",
                "subject_key": "act.detail_probe",
                "payload": {"label": "Detail Probe", "summary": "Has evidence."},
                "agent_confidence": 0.77,
            }
        ]
    )

    detail = await proposals.get_proposal(created["proposal_id"], SME)

    assert detail["status"] == "submitted"
    assert detail["gates_satisfied"] is False
    (item,) = detail["items"]
    assert item["subject_key"] == "act.detail_probe"
    assert item["original_payload"] is None, "nothing edited yet"
    assert item["item_status"] == "pending"

    # docs/10 §3: the reviewer must see what the claim is grounded in.
    (evidence,) = item["evidence"]
    assert evidence["source_id"] == seed["source_id"]
    assert evidence["source_kind"] == "interview"
    assert evidence["uri"] == "s3://fixtures/interview.txt"

    # docs/10 §6: which gate is open, and who is authorised to clear it.
    ontology = next(g for g in detail["gates"] if g["gate_kind"] == "ontology")
    roster = {r["principal"] for r in ontology["authorized"]}
    assert roster == {SME, COMPLIANCE, OWNER}
    assert all(r["has_decided"] is False for r in ontology["authorized"])
    assert ontology["live_decisions"] == []


async def test_decision_approve_reaches_quorum_and_approves(make_proposal: Any) -> None:
    created = make_proposal()

    first = await proposals.record_decision(
        _gate(created, "ontology"), SME, "approve", comment="fine", review_seconds=42
    )
    assert first["decision"]["decision"] == "approve"
    assert first["decision"]["review_seconds"] == 42
    # One gate cleared, one still open -> in_review, not approved.
    assert first["proposal"]["status"] == "in_review"
    assert first["proposal"]["gates_satisfied"] is False

    second = await proposals.record_decision(_gate(created, "factual"), SME, "approve")
    assert second["proposal"]["status"] == "approved"
    assert second["proposal"]["gates_satisfied"] is True
    assert second["proposal"]["decided_at"] is not None

    # `cleared_at` is the durable answer to "was this gate met" --
    # `hitl.record_decision` stamps it in the same transaction as the
    # decision, so the queue never has to re-derive the quorum rule.
    detail = await proposals.get_proposal(created["proposal_id"], SME)
    assert all(g["cleared_at"] is not None for g in detail["gates"])
    assert all(g["approvals"] >= g["quorum"] for g in detail["gates"])
    assert not any(g["blocked"] for g in detail["gates"])


async def test_decision_unauthorized_maps_to_400_verbatim(make_proposal: Any) -> None:
    """A refused decision must say WHY, in the database's own words.

    db/013's header is explicit that `gates_satisfied` has to be silent about
    an unauthorised decision because it is a predicate, so this layer is the
    only place a reviewer learns their click did nothing.
    """
    created = make_proposal()
    gate_id = _gate(created, "ontology")

    request = Request(
        method="POST",
        path=f"/api/gates/{gate_id}/decision",
        path_params={"gate_id": str(gate_id)},
        body={"decision": "approve"},
        principal=STRANGER,
    )
    response = await _dispatch(post_decision, request)

    assert response.status == HTTPStatus.BAD_REQUEST
    assert isinstance(response.body, dict)
    message = response.body["error"]
    assert STRANGER in message
    assert "holds no live ontology authority" in message
    # The message names the fix, and that half must survive too.
    assert "hitl.reviewer_authority" in message

    # An unknown principal is a DIFFERENT refusal with a different message.
    unknown = Request(
        method="POST",
        path=f"/api/gates/{gate_id}/decision",
        path_params={"gate_id": str(gate_id)},
        body={"decision": "approve"},
        principal="ghost@example.com",
    )
    unknown_response = await _dispatch(post_decision, unknown)
    assert unknown_response.status == HTTPStatus.BAD_REQUEST
    assert isinstance(unknown_response.body, dict)
    assert "is not a registered reviewer" in unknown_response.body["error"]


async def _dispatch(handler: Any, request: Any) -> Any:
    """Run one handler through the same error boundary the router applies."""
    try:
        return await handler(request)
    except Exception as exc:  # the boundary's job, reproduced for one handler
        return error_response(exc)


async def test_self_review_is_refused_when_the_gate_forbids_it(make_proposal: Any) -> None:
    """The agent's own principal cannot count toward its proposal's quorum."""
    created = make_proposal(authored_by=SME)  # the reviewer "authored" it

    with pytest.raises(pg_errors.RaiseException, match="this gate does not allow"):
        await proposals.record_decision(_gate(created, "ontology"), SME, "approve")


async def test_edit_item_then_merge_uses_edited_payload_and_labels_corrected(
    make_proposal: Any, sql: Any
) -> None:
    """The reviewer's edit is what reaches the graph, and what labels the trace.

    docs/07 calls the delta between what the agent proposed and what a human
    was willing to merge the most valuable event in the system. This asserts
    all three places it has to show up: `original_payload` on the item, the
    label on the node in kg, and 'corrected' on the training session.
    """
    session_id = str(uuid.uuid4())
    subject_key = f"act.edited_{uuid.uuid4().hex[:8]}"
    created = make_proposal(
        trace_session_id=session_id,
        items=[
            {
                "op": "add_node",
                "node_type": "activity",
                "subject_key": subject_key,
                "payload": {"label": "Agent's Wording", "summary": "As proposed."},
                "agent_confidence": 0.6,
            }
        ],
    )
    (item_id,) = created["item_ids"]

    edited = await proposals.edit_item(
        item_id, SME, {"label": "The Human's Wording", "summary": "As corrected."}
    )
    assert edited["item"]["item_status"] == "edited"
    assert edited["item"]["edited_by"] == SME
    assert edited["item"]["original_payload"]["label"] == "Agent's Wording"
    assert edited["item"]["payload"]["label"] == "The Human's Wording"

    await _clear_all_gates(created)
    result = await proposals.merge(created["proposal_id"], COMPLIANCE)

    assert result["proposal"]["status"] == "merged"
    assert result["commit"]["commit_id"] == result["proposal"]["merged_commit_id"]
    assert result["commit"]["sealed_by"] == COMPLIANCE

    # The edited label, not the agent's, is what the graph now holds.
    (node,) = sql(
        "SELECT label, summary FROM kg.node WHERE node_key = %(k)s AND valid_to IS NULL",
        {"k": subject_key},
    )
    assert node["label"] == "The Human's Wording"

    # 'corrected', not 'accepted' -- the agent was useful but wrong.
    (trace,) = sql(
        "SELECT outcome::text AS outcome, label_source, label_proposal_id "
        "FROM trn.trace_session WHERE session_id = %(s)s::uuid",
        {"s": session_id},
    )
    assert trace["outcome"] == "corrected"
    assert trace["label_source"] == "hitl_gate"
    assert trace["label_proposal_id"] == created["proposal_id"]


async def test_untouched_merge_labels_accepted(make_proposal: Any, sql: Any) -> None:
    """The other half of the label: nothing edited means 'accepted'."""
    session_id = str(uuid.uuid4())
    created = make_proposal(trace_session_id=session_id)

    await _clear_all_gates(created)
    await proposals.merge(created["proposal_id"], SME)

    (trace,) = sql(
        "SELECT outcome::text AS outcome FROM trn.trace_session WHERE session_id = %(s)s::uuid",
        {"s": session_id},
    )
    assert trace["outcome"] == "accepted"


async def test_merge_before_approved_409(make_proposal: Any) -> None:
    """Merging an unapproved proposal is a CONFLICT, not a bad request.

    Nothing about the request is malformed -- the resource is simply not in a
    state that permits the operation, and a client should be able to tell
    those apart without parsing prose.
    """
    created = make_proposal()

    with pytest.raises(GateError) as excinfo:
        await proposals.merge(created["proposal_id"], SME)

    assert excinfo.value.status == HTTPStatus.CONFLICT
    assert "expected approved" in excinfo.value.message

    # And it really did not merge.
    detail = await proposals.get_proposal(created["proposal_id"], SME)
    assert detail["status"] == "submitted"
    assert detail["merged_commit_id"] is None


async def test_reject_labels_the_trace_and_closes_the_proposal(
    make_proposal: Any, sql: Any
) -> None:
    session_id = str(uuid.uuid4())
    created = make_proposal(trace_session_id=session_id)

    result = await proposals.record_decision(
        _gate(created, "ontology"), SME, "reject", comment="not what we do"
    )
    assert result["proposal"]["status"] == "rejected"

    (trace,) = sql(
        "SELECT outcome::text AS outcome, label_source FROM trn.trace_session "
        "WHERE session_id = %(s)s::uuid",
        {"s": session_id},
    )
    assert trace["outcome"] == "rejected"
    assert trace["label_source"] == "hitl_gate"


async def test_expire_sweep_leaves_the_trace_label_pending(make_proposal: Any, sql: Any) -> None:
    """An expiry is an SLA breach, not a judgment -- so it must NOT label.

    A session labelled by an expiry would enter `trn.sft_export` carrying a
    verdict no human ever gave.
    """
    session_id = str(uuid.uuid4())
    created = make_proposal(trace_session_id=session_id, expires_at="2020-01-01T00:00:00Z")

    result = await proposals.expire()
    assert result["expired"] >= 1

    (row,) = sql(
        "SELECT status::text AS status FROM hitl.proposal WHERE proposal_id = %(p)s",
        {"p": created["proposal_id"]},
    )
    assert row["status"] == "expired"

    (trace,) = sql(
        "SELECT outcome::text AS outcome, label_source FROM trn.trace_session "
        "WHERE session_id = %(s)s::uuid",
        {"s": session_id},
    )
    assert trace["outcome"] == "pending"
    assert trace["label_source"] is None


async def test_queue_reports_the_authoring_agent(make_proposal: Any) -> None:
    """Who proposed this is part of the review, not metadata."""
    created = make_proposal()
    queue = await proposals.list_queue(SME)
    mine = next(p for p in queue["proposals"] if p["proposal_id"] == created["proposal_id"])
    assert mine["authored_by"] == AGENT_PRINCIPAL
    assert mine["agent_name"] == "engagement"
