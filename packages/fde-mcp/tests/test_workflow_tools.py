"""Tests for fde_mcp.tools.workflow -- faithful workflow authoring."""

from __future__ import annotations

from typing import Any

import pytest

from fde_mcp import db
from fde_mcp.tools import workflow

pytestmark = pytest.mark.requires_db


async def test_wf_draft_faithful_workflow_succeeds(seed: dict[str, Any]) -> None:
    steps = [
        workflow.StepSpec(
            step_key="review_contract",
            ordinal=1,
            kind="human",
            title="Legal reviews the contract",
            instruction="Read the contract and confirm terms are standard.",
            human_prompt="Are terms standard?",
            bindings=[
                workflow.StepBindingSpec(
                    subject_kind="node", subject_key="act.legal_review", relation="implements"
                )
            ],
        ),
        workflow.StepSpec(
            step_key="approve_discount",
            ordinal=2,
            kind="human",
            title="Deal desk approves the discount",
            instruction="Approve or reject the requested discount.",
            human_prompt="Approve?",
            bindings=[
                workflow.StepBindingSpec(
                    subject_kind="node", subject_key="act.discount_approval", relation="implements"
                )
            ],
        ),
    ]
    result = await workflow.wf_draft(
        engagement_id=seed["engagement_id"],
        slug="quote-to-cash-review",
        title="Quote to Cash Review",
        root_process_key="proc.quote_to_cash",
        pinned_commit_id=seed["commit_id"],
        steps=steps,
    )
    assert "error" not in result
    assert result["faithful"] is True
    assert result["status"] == "draft"

    fetched = await workflow.wf_get(result["workflow_id"])
    assert len(fetched["steps"]) == 2
    assert fetched["steps"][0]["bindings"]


async def test_wf_draft_unfaithful_workflow_rolls_back_and_raises_verbatim(
    seed: dict[str, Any],
) -> None:
    steps = [
        workflow.StepSpec(
            step_key="unbound_step",
            ordinal=1,
            kind="tool",
            title="An unbound step",
            instruction="This step cites no graph element.",
            bindings=[],  # no binding -- must fail wf.assert_faithful
        ),
    ]
    with pytest.raises(RuntimeError) as exc_info:
        await workflow.wf_draft(
            engagement_id=seed["engagement_id"],
            slug="unfaithful-workflow",
            title="Unfaithful Workflow",
            root_process_key="proc.quote_to_cash",
            pinned_commit_id=seed["commit_id"],
            steps=steps,
        )
    assert "unbound" in str(exc_info.value).lower()

    async with db.tool_transaction() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) AS n FROM wf.workflow WHERE engagement_id = %(eng)s::uuid AND slug = %(slug)s",
            {"eng": seed["engagement_id"], "slug": "unfaithful-workflow"},
        )
        row = await cur.fetchone()
    assert row is not None
    assert row["n"] == 0, (
        "a failed assert_faithful must roll back the whole draft, not leave a broken row"
    )


async def test_wf_list_and_get_not_found(seed: dict[str, Any]) -> None:
    result = await workflow.wf_list(engagement_id=seed["engagement_id"], status="draft")
    assert result["returned"] >= 1

    missing = await workflow.wf_get(999_999_999)
    assert "error" in missing
