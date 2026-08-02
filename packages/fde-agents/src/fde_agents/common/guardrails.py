"""Input/output guards shared by the Engagement, Workflow, and Development
agents.

Design intent
-------------
Every guard here returns a structured `Violation` (or an empty list) and
never raises. This mirrors `fde_mcp.tools._base`'s own error-handling
philosophy (`pg_error_boundary`'s docstring: a problem the caller can
plausibly fix should be surfaced as data the model can read and self-correct
on, not as an exception that unwinds the whole turn). A guardrail firing is
exactly that kind of problem -- "this proposal item looks like it names a
person" is information the agent should read and act on (drop the field, ask
for a role instead), not a crash.

These guards catch things the *database* cannot catch, which is precisely
why they exist as a second layer rather than being redundant with it:

* `no_person_name_in_role_label` -- `db/001_extensions_and_types.sql`'s "PII
  posture" section says the constraint against writing a person's name into
  a `role` node label is "a tripwire, not a guarantee -- pair it with
  review". This function IS that tripwire's application-layer half: a
  regex/heuristic check plus a deny-list of PII-ish payload keys, run before
  the item ever reaches `kg_propose`.

* `no_unbound_step` -- `wf.assert_faithful` (see `db/006_workflows.sql`)
  already refuses to publish an unbound step at the database layer, and
  refusal there rolls back the ENTIRE draft workflow (`wf_draft`'s
  docstring). Catching it here, per-step, before a single `wf_draft` call is
  made, means a 40-step workflow with one unbound step gets a precise,
  early, per-step error instead of an all-or-nothing rollback discovered
  only after every step was already built.

* `citation_required` -- there is no database constraint that can check "did
  the model's prose in this turn assert a graph fact it did not actually
  retrieve" -- `hitl.submit_proposal` only checks that proposal ITEMS carry
  `source_ids` (kg.source rows), which is a different, narrower thing than
  "every claim in the agent's own natural-language output is grounded in a
  `provenance`-bearing retrieval result from this session." This is squarely
  the kind of check 009's `trn.trace_step.grounded` column and the
  `ungrounded_claim` failure label exist to formalize for training -- this
  guard is the online, pre-hoc version of that same idea.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["block", "warn"]


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read `name` off `obj` whether it's a Pydantic model/object (attribute
    access) or a plain dict (as arrives when a guard is applied post-hoc to
    a tool call's raw JSON arguments, e.g. a `wf_draft` call's `steps`
    payload as the model actually sent it over MCP, rather than to our own
    typed `models.StepSpec`). Both call shapes are legitimate: an agent's
    own orchestration code that builds a `models.ProposalItem`/`StepSpec`
    before ever calling a tool gets attribute access; a guard applied to
    the tool call the model itself issued gets a dict.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass
class Violation:
    guard: str
    severity: Severity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Guard 1: no person names in a `role` node label / PII-ish payload keys.
# ---------------------------------------------------------------------------
# Deny-list of payload keys that should never appear on a node/edge proposal
# item at all -- their presence means someone is trying to smuggle an
# identity into the graph rather than a role, regardless of node_type. Kept
# broad (covers common SoR field names an agent might copy verbatim from an
# interview transcript or a Jira export) rather than narrow, because a
# false positive here just means "the human reviewing the ontology gate sees
# an extra warning", while a false negative means a name lands in the graph.
_PII_DENY_KEYS = frozenset(
    {
        "name",
        "full_name",
        "first_name",
        "last_name",
        "employee_name",
        "assignee_name",
        "reporter_name",
        "author_name",
        "email",
        "e_mail",
        "phone",
        "phone_number",
        "ssn",
        "employee_id",
        "user_id",
        "username",
        "slack_handle",
        "manager_name",
        "actor_name",
    }
)

# Heuristic for "this label/value looks like a person's name" rather than a
# role title. Intentionally simple and over-inclusive (title case, two-to-
# four space-separated word tokens, no role-ish keyword present) -- this is
# a tripwire per the module docstring, not a named-entity recognizer; a
# human reviewer at the ontology gate makes the real call.
_ROLE_KEYWORDS = re.compile(
    r"\b(manager|lead|director|analyst|specialist|associate|coordinator|"
    r"officer|engineer|representative|rep|clerk|administrator|admin|"
    r"supervisor|owner|agent|processor|reviewer|approver|team|department|"
    r"unit|desk|group|function|role)\b",
    re.IGNORECASE,
)
_NAME_LIKE = re.compile(
    r"^([A-Z][a-z]+(?:[-'][A-Z][a-z]+)?\s+){1,3}[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?$"
)


def no_person_name_in_role_label(item: Any) -> list[Violation]:
    """`item` is a `models.ProposalItem` (or anything with the same
    `.node_type` / `.payload` shape). Flags:

    (a) any PII-ish key present anywhere in the payload (recursively, since
        `attributes` is a nested jsonb blob an agent could bury a field in);
    (b) a `role`-typed node whose `label` looks like a person's name rather
        than a role title (name-shaped tokens AND no role keyword present).
    """
    violations: list[Violation] = []
    payload = _attr(item, "payload", None) or {}

    hit_keys = sorted(_find_pii_keys(payload))
    if hit_keys:
        violations.append(
            Violation(
                guard="no_person_name_in_role_label",
                severity="block",
                message=(
                    f"payload contains PII-ish key(s) {hit_keys} -- the graph models "
                    "roles and org units, never individual identities (see "
                    "db/001_extensions_and_types.sql's PII posture note); remove "
                    "these fields or replace them with a role_key/org_unit_key reference"
                ),
                detail={"subject_key": _attr(item, "subject_key"), "keys": hit_keys},
            )
        )

    node_type = _attr(item, "node_type")
    if node_type == "role":
        label = payload.get("label", "") if isinstance(payload, dict) else ""
        if isinstance(label, str) and label.strip() and _looks_like_person_name(label):
            violations.append(
                Violation(
                    guard="no_person_name_in_role_label",
                    severity="block",
                    message=(
                        f"role node label {label!r} looks like a person's name, not a "
                        "role title (e.g. 'AP Clerk', 'Regional Sales Manager'); a role "
                        "node must describe a job function, never an individual"
                    ),
                    detail={"subject_key": _attr(item, "subject_key"), "label": label},
                )
            )
    return violations


def _find_pii_keys(payload: Any, _seen_keys: set[str] | None = None) -> set[str]:
    hits: set[str] = _seen_keys if _seen_keys is not None else set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str) and key.lower() in _PII_DENY_KEYS:
                hits.add(key.lower())
            _find_pii_keys(value, hits)
    elif isinstance(payload, list | tuple):
        for value in payload:
            _find_pii_keys(value, hits)
    return hits


def _looks_like_person_name(label: str) -> bool:
    if _ROLE_KEYWORDS.search(label):
        return False
    return bool(_NAME_LIKE.match(label.strip()))


# ---------------------------------------------------------------------------
# Guard 2: refuse to emit a workflow step with no binding.
# ---------------------------------------------------------------------------
def no_unbound_step(step: Any) -> list[Violation]:
    """`step` is a `models.StepSpec` (or anything with `.kind`/`.bindings`).

    Mirrors `wf.assert_faithful`'s first rule exactly (`db/006_workflows.
    sql`): every step whose `kind != 'notify'` needs at least one binding.
    `notify` steps are legitimately unbound -- they are pure
    communication ("tell the requester their case is ready"), not an
    assertion that some graph element exists or was implemented.
    """
    kind = _attr(step, "kind")
    bindings = _attr(step, "bindings", None) or []
    if kind != "notify" and len(bindings) == 0:
        return [
            Violation(
                guard="no_unbound_step",
                severity="block",
                message=(
                    f"step {_attr(step, 'step_key', '?')!r} (kind={kind!r}) has no "
                    "graph binding -- every non-notify step must cite the kg node or "
                    "edge it implements/enforces/records_to/depends_on/measured_by "
                    "(wf.assert_faithful will reject the whole workflow draft otherwise)"
                ),
                detail={"step_key": _attr(step, "step_key"), "kind": kind},
            )
        ]
    return []


def no_unbound_steps(steps: Iterable[Any]) -> list[Violation]:
    """Batch form of `no_unbound_step` over a whole `WorkflowSpec.steps`."""
    violations: list[Violation] = []
    for step in steps:
        violations.extend(no_unbound_step(step))
    return violations


# ---------------------------------------------------------------------------
# Guard 3: citation_required -- refuse to let a turn assert a graph fact
# with no provenance from a retrieval result in the same session.
# ---------------------------------------------------------------------------
# A conservative pattern for "this sentence asserts something about the
# business" rather than hedging, asking a question, or describing process.
# Deliberately narrow (verbs of assertion about how things ARE, not modal/
# hedged language) to keep the false-positive rate low enough that this
# guard is usable turn-by-turn rather than something agents learn to route
# around with vague language.
_ASSERTION_PATTERN = re.compile(
    r"\b(is performed by|is owned by|is gated by|precedes|hands off to|"
    r"depends on|is recorded in|always happens|never happens|the process is|"
    r"the activity is|is required before|must occur before)\b",
    re.IGNORECASE,
)
# Hedged/sourced language that defuses the assertion pattern above -- if the
# model already says "according to X" or "I could not find", it is not
# making an ungrounded claim, it is either citing or explicitly declining.
_HEDGE_PATTERN = re.compile(
    r"\b(according to|per the (?:graph|source|interview|document)|"
    r"i (?:could not|couldn't|did not|didn't) find|no evidence|"
    r"based on (?:node|edge|source|retrieval))\b",
    re.IGNORECASE,
)


def citation_required(
    turn_text: str,
    *,
    session_provenance: Iterable[dict[str, Any]],
) -> list[Violation]:
    """Fail the turn if `turn_text` asserts a graph fact and
    `session_provenance` (the accumulated `provenance` blocks from every
    `kg_search`/`kg_traverse`/`kg_get_node` result returned so far this
    session) is empty.

    This is intentionally a coarse, whole-turn check, not a per-sentence
    fact-checker: it answers "has this agent retrieved ANYTHING it could be
    grounding assertions in, given that it is now making assertion-shaped
    statements?" A session with zero retrieval calls but assertion-shaped
    output is close to certainly fabricating; a session with retrieval calls
    already made is allowed through here (the DB-side `source_ids`
    requirement on `kg_propose` items is what enforces per-fact grounding
    for anything that becomes a graph proposal -- this guard's job is the
    earlier, narrative-output half of the same invariant, per the module
    docstring).
    """
    provenance_list = list(session_provenance)
    if not _ASSERTION_PATTERN.search(turn_text):
        return []
    if _HEDGE_PATTERN.search(turn_text):
        return []
    if provenance_list:
        return []
    return [
        Violation(
            guard="citation_required",
            severity="block",
            message=(
                "this turn asserts a fact about how the business operates but no "
                "kg_search/kg_traverse/kg_get_node result with a `provenance` block "
                "has been retrieved yet this session -- retrieve evidence before "
                "asserting, or hedge explicitly (e.g. 'I could not find evidence for...')"
            ),
            detail={"turn_excerpt": turn_text[:280]},
        )
    ]


# ---------------------------------------------------------------------------
# Convenience: run every guard applicable to a proposal item / step / turn.
# ---------------------------------------------------------------------------
def check_proposal_item(item: Any) -> list[Violation]:
    return no_person_name_in_role_label(item)


def check_workflow(steps: Iterable[Any]) -> list[Violation]:
    return no_unbound_steps(steps)


def check_turn(turn_text: str, *, session_provenance: Iterable[dict[str, Any]]) -> list[Violation]:
    return citation_required(turn_text, session_provenance=session_provenance)
