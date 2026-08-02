"""Tests for fde_mcp.tools.proposals -- human-in-the-loop change proposals."""

from __future__ import annotations

from typing import Any

import pytest

from fde_mcp import db
from fde_mcp.tools import proposals

pytestmark = pytest.mark.requires_db


# ===========================================================================
# kg_propose -- REQUIRED: returns required gates
# ===========================================================================
async def test_kg_propose_returns_required_gates(seed: dict[str, Any]) -> None:
    items = [
        proposals.ProposalItem(
            op="add_node",
            node_type="pain_point",
            subject_key="pain.manual_discount_chase",
            payload={
                "label": "Manual discount follow-up",
                "summary": "Deal desk manually chases approvers.",
            },
            source_ids=[seed["source_id"]],
            agent_confidence=0.8,
        )
    ]
    result = await proposals.kg_propose(
        engagement_id=seed["engagement_id"],
        title="Add pain point: manual discount chase",
        rationale="Observed in interview.",
        items=items,
        base_commit_id=seed["commit_id"],
    )
    assert "error" not in result
    assert result["proposal_id"] > 0
    assert result["status"] == "draft"
    assert result["required_gates"], "the catch-all ontology policy should always fire"
    assert any(g["gate_kind"] == "ontology" for g in result["required_gates"])


async def test_kg_propose_control_edge_requires_control_gate(seed: dict[str, Any]) -> None:
    items = [
        proposals.ProposalItem(
            op="add_edge",
            edge_type="gated_by",
            subject_key="act.legal_review|gated_by|ctrl.new_control",
            payload={"src_key": "act.legal_review", "dst_key": "ctrl.discount_threshold"},
            source_ids=[seed["source_id"]],
            agent_confidence=0.9,
        )
    ]
    result = await proposals.kg_propose(
        engagement_id=seed["engagement_id"],
        title="Gate legal review on the discount control too",
        rationale="Testing gate computation for gated_by edges.",
        items=items,
        base_commit_id=seed["commit_id"],
    )
    kinds = {g["gate_kind"] for g in result["required_gates"]}
    assert "control" in kinds, "gated_by edges must trigger the compliance control gate (policy #3)"


# ===========================================================================
# kg_submit_proposal -- REQUIRED: fails loudly with no evidence
# ===========================================================================
async def test_kg_submit_proposal_fails_loudly_with_no_evidence(seed: dict[str, Any]) -> None:
    items = [
        proposals.ProposalItem(
            op="add_node",
            node_type="pain_point",
            subject_key="pain.unsourced_claim",
            payload={"label": "An unsourced claim"},
            source_ids=[],  # <-- no evidence, on purpose
            agent_confidence=0.5,
        )
    ]
    proposed = await proposals.kg_propose(
        engagement_id=seed["engagement_id"],
        title="Proposal with no evidence",
        rationale="Should be rejected at submit time.",
        items=items,
        base_commit_id=seed["commit_id"],
    )
    proposal_id = proposed["proposal_id"]

    with pytest.raises(RuntimeError) as exc_info:
        await proposals.kg_submit_proposal(proposal_id)

    message = str(exc_info.value)
    assert "evidence" in message.lower(), f"expected the RAISE message verbatim, got: {message!r}"
    assert str(proposal_id) in message

    # Confirm it truly did not submit -- still 'draft'.
    async with db.tool_transaction() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM hitl.proposal WHERE proposal_id = %(pid)s", {"pid": proposal_id}
        )
        row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "draft"


async def test_kg_submit_proposal_succeeds_with_evidence_and_freezes_gates(
    seed: dict[str, Any],
) -> None:
    items = [
        proposals.ProposalItem(
            op="add_node",
            node_type="pain_point",
            subject_key="pain.sourced_claim",
            payload={"label": "A properly sourced claim"},
            source_ids=[seed["source_id"]],
            agent_confidence=0.9,
        )
    ]
    proposed = await proposals.kg_propose(
        engagement_id=seed["engagement_id"],
        title="Proposal with evidence",
        rationale="Should submit cleanly.",
        items=items,
        base_commit_id=seed["commit_id"],
    )
    submitted = await proposals.kg_submit_proposal(proposed["proposal_id"])
    assert submitted["proposal"]["status"] == "submitted"
    assert submitted["gates"], "gates must be frozen at submit time"
    for gate in submitted["gates"]:
        assert gate["due_at"] is not None


# ===========================================================================
# kg_proposal_status
# ===========================================================================
async def test_kg_proposal_status_reports_unsatisfied_gates(seed: dict[str, Any]) -> None:
    items = [
        proposals.ProposalItem(
            op="add_node",
            node_type="pain_point",
            subject_key="pain.status_check",
            payload={"label": "Status check claim"},
            source_ids=[seed["source_id"]],
            agent_confidence=0.7,
        )
    ]
    proposed = await proposals.kg_propose(
        engagement_id=seed["engagement_id"],
        title="Status check",
        rationale="r",
        items=items,
        base_commit_id=seed["commit_id"],
    )
    await proposals.kg_submit_proposal(proposed["proposal_id"])
    status = await proposals.kg_proposal_status(proposed["proposal_id"])
    assert status["overall_satisfied"] is False  # no reviewer has approved yet
    assert status["gates"]
    assert status["gates"][0]["quorum_met"] is False


async def test_kg_proposal_status_not_found() -> None:
    result = await proposals.kg_proposal_status(2_000_000_000)
    assert "error" in result
