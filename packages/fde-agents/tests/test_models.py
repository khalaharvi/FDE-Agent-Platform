"""Unit tests for fde_agents.common.models.

Focused on `OpportunityScore` (the composite arithmetic, the band
boundaries, and both hard floors), plus a couple of validation/shape checks
on `ProposalItem`/`StepSpec` that the guardrail tests rely on implicitly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fde_agents.common.models import OpportunityScore, ProposalItem, StepSpec


def _score(**overrides: int) -> OpportunityScore:
    defaults: dict[str, int] = {
        "volume": 3,
        "standardisation": 3,
        "data_availability": 3,
        "decision_complexity": 3,
        "error_tolerance": 3,
        "control_exposure": 3,
    }
    defaults.update(overrides)
    return OpportunityScore(activity_key="act.x", **defaults)


# ---------------------------------------------------------------------------
# composite arithmetic
# ---------------------------------------------------------------------------
def test_composite_is_the_documented_weighted_sum() -> None:
    score = OpportunityScore(
        activity_key="act.x",
        volume=5,
        standardisation=4,
        data_availability=3,
        decision_complexity=2,
        error_tolerance=1,
        control_exposure=5,
    )
    expected = round(
        0.20 * 5 + 0.20 * 4 + 0.15 * 3 + 0.20 * 2 + 0.15 * 1 + 0.10 * 5,
        3,
    )
    assert score.composite == expected


def test_composite_of_all_fives_is_five() -> None:
    assert (
        _score(
            volume=5,
            standardisation=5,
            data_availability=5,
            decision_complexity=5,
            error_tolerance=5,
            control_exposure=5,
        ).composite
        == 5.0
    )


def test_composite_of_all_ones_is_one() -> None:
    assert (
        _score(
            volume=1,
            standardisation=1,
            data_availability=1,
            decision_complexity=1,
            error_tolerance=1,
            control_exposure=1,
        ).composite
        == 1.0
    )


# ---------------------------------------------------------------------------
# band boundaries
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("volume", "standardisation", "expected_band"),
    [
        (5, 5, "automate_now"),  # composite well above 4.0
        (3, 3, "automate_with_human_review"),  # composite in [3.0, 4.0)
        (2, 2, "augment_only"),  # composite in [2.0, 3.0)
        (1, 1, "do_not_automate"),  # composite below 2.0
    ],
)
def test_band_matches_composite_range(
    volume: int, standardisation: int, expected_band: str
) -> None:
    score = _score(
        volume=volume,
        standardisation=standardisation,
        data_availability=volume,
        decision_complexity=3,  # kept clear of the decision_complexity<=1 floor
        error_tolerance=volume,
        control_exposure=volume,
    )
    assert score.band == expected_band


def test_band_boundary_exactly_four_is_automate_now() -> None:
    # composite == 4.0 exactly at all dimensions == 4
    score = _score(
        volume=4,
        standardisation=4,
        data_availability=4,
        decision_complexity=4,
        error_tolerance=4,
        control_exposure=4,
    )
    assert score.composite == 4.0
    assert score.band == "automate_now"


def test_band_boundary_exactly_three_is_automate_with_human_review() -> None:
    score = _score(
        volume=3,
        standardisation=3,
        data_availability=3,
        decision_complexity=3,
        error_tolerance=3,
        control_exposure=3,
    )
    assert score.composite == 3.0
    assert score.band == "automate_with_human_review"


def test_band_boundary_exactly_two_is_augment_only() -> None:
    score = _score(
        volume=2,
        standardisation=2,
        data_availability=2,
        decision_complexity=2,
        error_tolerance=2,
        control_exposure=2,
    )
    assert score.composite == 2.0
    assert score.band == "augment_only"


# ---------------------------------------------------------------------------
# hard floors -- a single catastrophic dimension overrides the composite
# ---------------------------------------------------------------------------
def test_control_exposure_floor_overrides_a_high_composite() -> None:
    score = _score(
        volume=5,
        standardisation=5,
        data_availability=5,
        decision_complexity=5,
        error_tolerance=5,
        control_exposure=1,
    )
    assert score.composite >= 4.0  # would be automate_now on the number alone
    assert score.band == "do_not_automate"


def test_decision_complexity_floor_overrides_a_high_composite() -> None:
    score = _score(
        volume=5,
        standardisation=5,
        data_availability=5,
        decision_complexity=1,
        error_tolerance=5,
        control_exposure=5,
    )
    assert score.composite >= 4.0
    assert score.band == "do_not_automate"


def test_floors_do_not_fire_above_the_threshold() -> None:
    # control_exposure=2 and decision_complexity=2 are both above the <=1
    # floor, so a high composite should NOT be forced to do_not_automate.
    score = _score(
        volume=5,
        standardisation=5,
        data_availability=5,
        decision_complexity=5,
        error_tolerance=5,
        control_exposure=5,
    )
    assert score.band != "do_not_automate"


# ---------------------------------------------------------------------------
# ProposalItem / StepSpec -- shape checks the guardrail tests lean on.
# ---------------------------------------------------------------------------
def test_proposal_item_rejects_node_op_without_node_type() -> None:
    with pytest.raises(ValidationError):
        ProposalItem(op="add_node", subject_key="x", agent_confidence=0.5)


def test_proposal_item_rejects_node_op_with_edge_type_also_set() -> None:
    with pytest.raises(ValidationError):
        ProposalItem(
            op="add_node",
            node_type="role",
            edge_type="performs",
            subject_key="x",
            agent_confidence=0.5,
        )


def test_proposal_item_to_mcp_args_shape() -> None:
    item = ProposalItem(
        op="add_node",
        node_type="role",
        subject_key="role.ap_clerk",
        payload={"label": "AP Clerk"},
        source_ids=[1, 2],
        agent_confidence=0.75,
    )
    args = item.to_mcp_args()
    assert args["op"] == "add_node"
    assert args["node_type"] == "role"
    assert args["source_ids"] == [1, 2]
    assert "provenance_refs" not in args  # session-local bookkeeping, not sent to the DB


def test_step_spec_notify_defaults_to_no_bindings_required() -> None:
    step = StepSpec(
        step_key="notify.customer",
        ordinal=1,
        kind="notify",
        title="Notify",
        instruction="Tell the customer.",
    )
    assert step.bindings == []
