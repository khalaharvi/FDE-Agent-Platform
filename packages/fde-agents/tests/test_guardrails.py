"""Unit tests for fde_agents.common.guardrails.

Every guard is exercised both positive (fires, `block` severity, correct
`guard` name) and negative (does not fire on an obviously clean input), per
each guard's own docstring contract: a guard returns a list of `Violation`,
never raises, and never fires on input it has no business flagging.
"""

from __future__ import annotations

from fde_agents.common import guardrails
from fde_agents.common.models import ProposalItem, StepBindingSpec, StepSpec


def _proposal_item(**overrides: object) -> ProposalItem:
    defaults: dict[str, object] = {
        "op": "add_node",
        "node_type": "role",
        "subject_key": "role.ap_clerk",
        "payload": {"label": "AP Clerk"},
        "agent_confidence": 0.9,
    }
    defaults.update(overrides)
    return ProposalItem(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# no_person_name_in_role_label
# ---------------------------------------------------------------------------
def test_no_person_name_flags_pii_key_in_payload() -> None:
    item = _proposal_item(payload={"label": "AP Clerk", "employee_name": "Jane Smith"})
    violations = guardrails.no_person_name_in_role_label(item)
    assert len(violations) == 1
    assert violations[0].guard == "no_person_name_in_role_label"
    assert violations[0].severity == "block"
    assert "employee_name" in violations[0].detail["keys"]


def test_no_person_name_flags_pii_key_nested_in_payload() -> None:
    item = _proposal_item(payload={"label": "AP Clerk", "attributes": {"manager_name": "J. Lee"}})
    violations = guardrails.no_person_name_in_role_label(item)
    assert any(v.guard == "no_person_name_in_role_label" for v in violations)


def test_no_person_name_flags_name_shaped_role_label() -> None:
    item = _proposal_item(node_type="role", payload={"label": "Jane Smith"})
    violations = guardrails.no_person_name_in_role_label(item)
    assert len(violations) == 1
    assert violations[0].detail["label"] == "Jane Smith"


def test_no_person_name_allows_clean_role_label() -> None:
    item = _proposal_item(node_type="role", payload={"label": "Regional Sales Manager"})
    assert guardrails.no_person_name_in_role_label(item) == []


def test_no_person_name_allows_non_role_node_with_name_shaped_label() -> None:
    # A "system" node named "Jane Smith" is unusual but not a PII violation --
    # the name-shape heuristic only applies to role-typed nodes.
    item = _proposal_item(node_type="system", payload={"label": "Jane Smith"})
    assert guardrails.no_person_name_in_role_label(item) == []


def test_no_person_name_works_on_plain_dict_not_just_pydantic_model() -> None:
    raw = {"node_type": "role", "subject_key": "role.x", "payload": {"label": "John Doe"}}
    violations = guardrails.no_person_name_in_role_label(raw)
    assert len(violations) == 1


# ---------------------------------------------------------------------------
# no_unbound_step / no_unbound_steps
# ---------------------------------------------------------------------------
def _step(**overrides: object) -> StepSpec:
    defaults: dict[str, object] = {
        "step_key": "step.review",
        "ordinal": 1,
        "kind": "human",
        "title": "Review",
        "instruction": "Review the case.",
        "bindings": [
            StepBindingSpec(subject_kind="node", subject_key="act.review", relation="implements")
        ],
    }
    defaults.update(overrides)
    return StepSpec(**defaults)  # type: ignore[arg-type]


def test_no_unbound_step_flags_non_notify_step_with_no_bindings() -> None:
    step = _step(bindings=[])
    violations = guardrails.no_unbound_step(step)
    assert len(violations) == 1
    assert violations[0].guard == "no_unbound_step"
    assert violations[0].detail["step_key"] == "step.review"


def test_no_unbound_step_allows_bound_step() -> None:
    step = _step()
    assert guardrails.no_unbound_step(step) == []


def test_no_unbound_step_allows_unbound_notify_step() -> None:
    step = _step(kind="notify", bindings=[])
    assert guardrails.no_unbound_step(step) == []


def test_no_unbound_steps_batches_over_a_workflow() -> None:
    steps = [
        _step(step_key="a"),
        _step(step_key="b", bindings=[]),
        _step(kind="notify", bindings=[]),
    ]
    violations = guardrails.no_unbound_steps(steps)
    assert [v.detail["step_key"] for v in violations] == ["b"]


def test_no_unbound_step_works_on_plain_dict() -> None:
    raw = {"step_key": "s1", "kind": "tool", "bindings": []}
    violations = guardrails.no_unbound_step(raw)
    assert len(violations) == 1


# ---------------------------------------------------------------------------
# citation_required
# ---------------------------------------------------------------------------
def test_citation_required_fires_on_assertion_with_no_provenance() -> None:
    text = "Discount Approval is performed by the Deal Desk Analyst role."
    violations = guardrails.citation_required(text, session_provenance=[])
    assert len(violations) == 1
    assert violations[0].guard == "citation_required"


def test_citation_required_allows_assertion_with_provenance_present() -> None:
    text = "Discount Approval is performed by the Deal Desk Analyst role."
    violations = guardrails.citation_required(
        text, session_provenance=[{"rank": 1, "evidence_strength": 0.8}]
    )
    assert violations == []


def test_citation_required_allows_hedged_language_even_with_no_provenance() -> None:
    text = "According to the interview, Discount Approval is performed by Deal Desk."
    assert guardrails.citation_required(text, session_provenance=[]) == []


def test_citation_required_allows_non_assertion_text() -> None:
    text = "Could you clarify who signs off on this step?"
    assert guardrails.citation_required(text, session_provenance=[]) == []


def test_citation_required_allows_explicit_no_evidence_statement() -> None:
    text = "I could not find evidence that this activity precedes the next one."
    assert guardrails.citation_required(text, session_provenance=[]) == []


# ---------------------------------------------------------------------------
# convenience wrappers
# ---------------------------------------------------------------------------
def test_check_proposal_item_delegates_to_no_person_name() -> None:
    item = _proposal_item(node_type="role", payload={"label": "Jane Smith"})
    assert guardrails.check_proposal_item(item) == guardrails.no_person_name_in_role_label(item)


def test_check_workflow_delegates_to_no_unbound_steps() -> None:
    steps = [_step(bindings=[])]
    assert guardrails.check_workflow(steps) == guardrails.no_unbound_steps(steps)


def test_check_turn_delegates_to_citation_required() -> None:
    text = "The process is always the same."
    assert guardrails.check_turn(text, session_provenance=[]) == guardrails.citation_required(
        text, session_provenance=[]
    )
