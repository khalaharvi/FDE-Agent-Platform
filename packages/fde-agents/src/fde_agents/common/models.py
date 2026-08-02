"""Shared Pydantic models for the Engagement, Workflow, and Development agents.

These models exist for one reason: the three agent runtimes reason about,
build up incrementally, and stream partial versions of the same handful of
shapes (a graph-change proposal, a drift signal, a workflow-in-progress, an
opportunity score) well before those shapes are handed to an MCP tool call.
`fde_mcp.server` has its own Pydantic argument models (`ProposalItem`,
`StepSpec`, `StepBindingSpec`) that are the wire contract for `kg_propose` /
`wf_draft` -- those are intentionally not imported here, because an agent
process should not depend on the MCP server's Python module (it may be
reached over the Gateway as a completely separate process/service in
production). Instead, each model below defines a `to_mcp_args()` method that
produces exactly the dict shape those tools expect, so the two schemas are
kept in sync by a single, explicit, testable conversion point rather than by
accidental structural identity.

Every model is frozen-by-convention-not-by-force (mutation is allowed,
because agents build these up field-by-field while reasoning) but validates
eagerly, so a malformed proposal item fails at construction time inside the
agent -- long before it becomes a confusing Postgres CHECK-constraint error
surfaced back through `fde_mcp.tools._base.pg_error_boundary`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Closed ontology -- mirrors db/001_extensions_and_types.sql exactly. Kept as
# plain string Literals (not an import from the MCP server) so this module
# has zero runtime dependency on fde_mcp.server's module graph.
# ---------------------------------------------------------------------------
NODE_TYPES = (
    "org_unit",
    "role",
    "system",
    "system_object",
    "capability",
    "process",
    "activity",
    "artifact",
    "decision",
    "control",
    "metric",
    "pain_point",
    "opportunity",
    "tool_binding",
    "evidence_doc",
)
EDGE_TYPES = (
    "belongs_to",
    "performs",
    "precedes",
    "hands_off_to",
    "produces",
    "consumes",
    "recorded_in",
    "depends_on",
    "gated_by",
    "measured_by",
    "blocks",
    "addresses",
    "automatable_by",
    "evidenced_by",
    "supersedes",
)
NodeType = Literal[NODE_TYPES]  # type: ignore[valid-type]
EdgeType = Literal[EDGE_TYPES]  # type: ignore[valid-type]

PROPOSAL_OPS = ("add_node", "update_node", "retire_node", "add_edge", "update_edge", "retire_edge")
GATE_KINDS = ("ontology", "factual", "control", "automation")
STEP_KINDS = ("agent", "tool", "human", "decision", "sor_write", "notify")
STEP_BINDING_RELATIONS = ("implements", "enforces", "records_to", "depends_on", "measured_by")
DRIFT_KINDS = (
    "missing_in_sor",
    "missing_in_graph",
    "sequence_drift",
    "actor_drift",
    "latency_drift",
    "volume_drift",
    "control_bypass",
    "stale_pin",
)
DRIFT_SEVERITIES = ("info", "low", "medium", "high", "critical")


class ProposalItem(BaseModel):
    """One staged mutation of a `hitl.proposal`, matching `hitl.proposal_item`.

    `source_ids` is deliberately not defaulted to a non-empty requirement
    here -- an agent legitimately builds an item before it has resolved
    evidence, and `guardrails.citation_required` / the DB's
    `hitl.submit_proposal` are where the "no unsourced assertion" invariant
    is actually enforced. Validating it here too would just mean the same
    check exists in two places and drifts.
    """

    op: Literal[PROPOSAL_OPS]  # type: ignore[valid-type]
    node_type: NodeType | None = None
    edge_type: EdgeType | None = None
    subject_key: str = Field(description="node_key or edge_key being created/changed")
    payload: dict[str, Any] = Field(default_factory=dict)
    supersedes_key: str | None = None
    source_ids: list[int] = Field(default_factory=list)
    agent_confidence: float = Field(ge=0.0, le=1.0)
    # Which kg_search / kg_traverse / kg_get_node result this item's payload
    # was grounded in, keyed by that tool call's returned `provenance`. Not
    # sent to the DB (hitl.proposal_item has no such column) -- this is the
    # in-session bookkeeping `guardrails.citation_required` inspects.
    provenance_refs: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _op_matches_type(self) -> ProposalItem:
        is_node_op = self.op.endswith("node")
        if is_node_op and (self.node_type is None or self.edge_type is not None):
            raise ValueError(f"op={self.op!r} requires node_type set and edge_type unset")
        if not is_node_op and (self.edge_type is None or self.node_type is not None):
            raise ValueError(f"op={self.op!r} requires edge_type set and node_type unset")
        return self

    def to_mcp_args(self) -> dict[str, Any]:
        """Shape expected by `kg_propose`'s `items: list[ProposalItem]` arg."""
        return {
            "op": self.op,
            "node_type": self.node_type,
            "edge_type": self.edge_type,
            "subject_key": self.subject_key,
            "payload": self.payload,
            "supersedes_key": self.supersedes_key,
            "source_ids": self.source_ids,
            "agent_confidence": self.agent_confidence,
        }


class GateReviewerSummary(BaseModel):
    principal: str
    display_name: str


class GateSummary(BaseModel):
    """One row of `kg_proposal_status`'s per-gate breakdown, typed for the
    Workflow/Engagement agent's own reasoning (e.g. deciding what to tell a
    human about why a proposal is stuck).
    """

    gate_id: int
    gate_kind: Literal[GATE_KINDS]  # type: ignore[valid-type]
    quorum: int
    allow_self: bool
    triggering_items: list[int]
    due_at: datetime
    cleared_at: datetime | None = None
    distinct_approvals: int = 0
    quorum_met: bool = False
    has_blocking_decision: bool = False
    still_needed_reviewers: list[GateReviewerSummary] = Field(default_factory=list)

    @property
    def is_overdue(self) -> bool:
        return self.cleared_at is None and datetime.now(self.due_at.tzinfo) > self.due_at


class DriftSignal(BaseModel):
    """One row of `drift_list`, typed for the Workflow Agent's triage logic.

    `sample_size` / `effect_size` mirror `sor.drift_signal` exactly (both are
    nullable there too -- some drift kinds, notably `stale_pin`, do not carry
    a statistical sample). The Workflow Agent's triage rules (see
    `workflow/prompt.py`) key their minimum-sample-size and severity
    thresholds off these two fields.
    """

    signal_id: int
    drift_kind: Literal[DRIFT_KINDS]  # type: ignore[valid-type]
    severity: Literal[DRIFT_SEVERITIES]  # type: ignore[valid-type]
    state: str
    subject_kind: Literal["node", "edge", "workflow", "control"]
    subject_ref: str
    detail: dict[str, Any]
    sample_size: int | None = None
    effect_size: float | None = None
    detected_at: datetime
    last_seen_at: datetime
    occurrences: int = 1

    def meets_min_samples(self, min_samples: int) -> bool:
        """False (not True) when sample_size is unknown -- absence of a
        sample size is treated as "cannot yet judge significance", not as
        "assume it clears the bar". `stale_pin` signals, which never carry a
        sample size, are handled by a dedicated triage rule instead of this
        helper.
        """
        return self.sample_size is not None and self.sample_size >= min_samples


class StepBindingSpec(BaseModel):
    """Mirrors the MCP server's `StepBindingSpec` / `wf.step_binding`."""

    subject_kind: Literal["node", "edge"]
    subject_key: str
    relation: Literal[STEP_BINDING_RELATIONS]  # type: ignore[valid-type]

    def to_mcp_args(self) -> dict[str, Any]:
        return {
            "subject_kind": self.subject_kind,
            "subject_key": self.subject_key,
            "relation": self.relation,
        }


class StepSpec(BaseModel):
    """Mirrors the MCP server's `StepSpec` / `wf.step` + its bindings.

    `bindings` defaults empty because `notify` steps are legitimately
    unbound (see `wf.assert_faithful`'s first rule); every other `kind`
    needs >= 1 entry or the Workflow Agent's `wf_draft` call will be rolled
    back wholesale by the database. `guardrails.no_unbound_step` enforces
    this client-side too, before the round trip.
    """

    step_key: str
    ordinal: int
    kind: Literal[STEP_KINDS]  # type: ignore[valid-type]
    title: str
    instruction: str
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    human_prompt: str | None = None
    human_schema: dict[str, Any] | None = None
    branches: dict[str, Any] | None = None
    sor_adapter_key: str | None = None
    sor_write_op: str | None = None
    requires_human: bool = False
    timeout_seconds: int = 900
    on_failure: Literal["halt", "retry", "skip", "escalate"] = "halt"
    bindings: list[StepBindingSpec] = Field(default_factory=list)

    def to_mcp_args(self) -> dict[str, Any]:
        return {
            "step_key": self.step_key,
            "ordinal": self.ordinal,
            "kind": self.kind,
            "title": self.title,
            "instruction": self.instruction,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "human_prompt": self.human_prompt,
            "human_schema": self.human_schema,
            "branches": self.branches,
            "sor_adapter_key": self.sor_adapter_key,
            "sor_write_op": self.sor_write_op,
            "requires_human": self.requires_human,
            "timeout_seconds": self.timeout_seconds,
            "on_failure": self.on_failure,
            "bindings": [b.to_mcp_args() for b in self.bindings],
        }


class WorkflowSpec(BaseModel):
    """A draft workflow as the Workflow Agent assembles it, before calling
    `wf_draft`. Mirrors `wf.workflow` header fields plus its ordered steps.
    """

    engagement_id: str
    slug: str
    title: str
    root_process_key: str
    pinned_commit_id: int
    steps: list[StepSpec] = Field(default_factory=list)

    def to_mcp_args(self) -> dict[str, Any]:
        return {
            "engagement_id": self.engagement_id,
            "slug": self.slug,
            "title": self.title,
            "root_process_key": self.root_process_key,
            "pinned_commit_id": self.pinned_commit_id,
            "steps": [s.to_mcp_args() for s in self.steps],
        }


# ---------------------------------------------------------------------------
# AI opportunity scoring (Engagement Agent). See engagement/prompt.py for
# the full rubric this model's fields correspond to.
# ---------------------------------------------------------------------------
class OpportunityScore(BaseModel):
    """The six-dimension AI-opportunity rubric, 1-5 per dimension, plus the
    weighted composite and the resulting automation-recommendation band.

    Weights and the "do not automate" cutoff are defined once here (not
    reimplemented per-call in the agent loop) so that every score the
    Engagement Agent produces in a session is computed identically, and so a
    reviewer auditing a `kg_propose` for an `opportunity` node can recompute
    the composite from the six stored dimension scores without needing the
    model in the loop -- the arithmetic is deterministic, only the 1-5
    judgement calls are the model's.
    """

    activity_key: str = Field(description="kg node_key of the activity this scores")
    volume: int = Field(ge=1, le=5, description="How often does this activity occur?")
    standardisation: int = Field(
        ge=1, le=5, description="How consistent is the procedure across cases?"
    )
    data_availability: int = Field(
        ge=1, le=5, description="How complete/accessible is the data needed to perform it?"
    )
    decision_complexity: int = Field(
        ge=1,
        le=5,
        description="How much judgement vs. rule-following is required? (5 = pure rule-following)",
    )
    error_tolerance: int = Field(
        ge=1, le=5, description="How costly is a mistake? (5 = cheap/reversible)"
    )
    control_exposure: int = Field(
        ge=1,
        le=5,
        description="How much does this activity touch a compliance control? (5 = none)",
    )
    rationale: dict[str, str] = Field(
        default_factory=dict,
        description="Per-dimension one-line justification, keyed by dimension name. Required before submission.",
    )

    # Weights sum to 1.0. Volume and standardisation dominate because they
    # are necessary-but-not-sufficient: a high-volume, standardised task with
    # poor data availability or high decision complexity is not a good first
    # automation candidate, but a low-volume task is never worth the
    # integration cost regardless of how automatable it looks otherwise.
    _WEIGHTS = {
        "volume": 0.20,
        "standardisation": 0.20,
        "data_availability": 0.15,
        "decision_complexity": 0.20,
        "error_tolerance": 0.15,
        "control_exposure": 0.10,
    }

    @property
    def composite(self) -> float:
        total = sum(float(getattr(self, dim)) * weight for dim, weight in self._WEIGHTS.items())
        return round(total, 3)

    @property
    def band(self) -> str:
        """Matches the rubric's four bands exactly (see prompt.py). The
        "do not automate" floor exists so a low composite driven by ONE
        catastrophic dimension (e.g. control_exposure=1, meaning this sits
        directly inside a compliance control) cannot be averaged away by
        otherwise-strong dimensions.
        """
        if self.control_exposure <= 1 or self.decision_complexity <= 1:
            return "do_not_automate"
        c = self.composite
        if c >= 4.0:
            return "automate_now"
        if c >= 3.0:
            return "automate_with_human_review"
        if c >= 2.0:
            return "augment_only"
        return "do_not_automate"
