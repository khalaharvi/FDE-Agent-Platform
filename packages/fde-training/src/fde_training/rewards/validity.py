"""rewards/validity.py -- structural and value-level correctness of the
tool calls an episode issued.

Failure mode this guards against
----------------------------------
A policy under RL exploration will, by construction, emit malformed and
adversarial tool calls -- that IS what exploration means. Without a reward
term that directly scores "did this call even parse, and were its
arguments in range", the policy has no local gradient telling it a
malformed call is bad UNTIL the downstream retrieval/outcome reward also
happens to suffer, which can be many steps later or not at all (a
malformed `kg_traverse` call that silently no-ops still lets the policy
answer from whatever it already retrieved). `r_format`/`r_schema_valid`
give that signal immediately, every turn.

Two separate terms, not one, on purpose: a malformed CALL (bad JSON,
unknown tool name -- `r_format`'s job) is a much more severe failure than a
well-formed call with an out-of-range argument (`r_schema_valid`'s job),
and conflating them would let a model "average out" one kind of mistake
against the other during optimisation. KG-R1 (2509.26383) mixes turn-level
(format/query-validity/answer-format) and global (outcome/retrieval-
relevance) rewards; `r_format`/`r_schema_valid` are this pipeline's
turn-level half of that split.

Schema constants below mirror `fde_mcp.tools._base`'s NODE_TYPES/EDGE_TYPES
and the `kg.traverse`/`kg.hybrid_search` argument ceilings from
`db/008_retrieval.sql`. Duplicated here (not imported from `fde_mcp`) on
purpose: an RL trainer process is commonly a different container/image than
the MCP server (a GPU training image has no business installing
`psycopg`/`fastmcp` just to read two tuples), and reward functions must be
importable with ZERO application dependencies for `pytest` to exercise them
in isolation. Keep these in sync with `fde_mcp.tools._base` by hand; the
package's test suite includes a same-repo consistency check that fails
loudly if they drift (see `test_config.py::test_schema_constants_match_fde_mcp`).
"""

from __future__ import annotations

from typing import Any

from fde_training.rewards._episode import episode_logs

NODE_TYPES = frozenset(
    {
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
    }
)
EDGE_TYPES = frozenset(
    {
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
    }
)
KNOWN_TOOL_NAMES = frozenset(
    {
        "kg_head_commit",
        "kg_as_of",
        "kg_search",
        "kg_lexical_search",
        "kg_get_node",
        "kg_traverse",
        "kg_dependency_closure",
        "kg_impact_radius",
        "kg_process_flow",
        "kg_propose",
        "kg_submit_proposal",
        "kg_proposal_status",
        "drift_scan",
        "drift_list",
        "drift_triage",
        "wf_list",
        "wf_get",
        "wf_draft",
    }
)
MAX_HOPS_CEILING = 6  # kg.traverse's hard RAISE ceiling
MAX_NODES_CEILING = 5000  # kg.traverse's hard RAISE ceiling
MAX_SEARCH_K = 200  # kg_search's Field(le=200)
MAX_LEXICAL_K = 100  # kg_lexical_search's Field(le=100)
VALID_DIRECTIONS = frozenset({"out", "in", "both"})


# ===========================================================================
# r_format -- tool calls parse and validate against the MCP schema (purely
# STRUCTURAL: well-formed JSON, known tool name, required top-level shape).
# Value-level validity (enums, numeric ceilings) is r_schema_valid's job --
# see module docstring for why these are deliberately separate terms.
# ===========================================================================
def r_format(completions: list[Any], **kwargs: Any) -> list[float]:
    rewards = []
    for log in episode_logs(completions, kwargs):
        total = log.tool_call_count + log.malformed_tool_call_count
        if total == 0:
            # No tool calls attempted at all is neither a format success nor
            # failure -- efficiency.r_hop_efficiency penalises
            # under-retrieval, this term stays neutral so it doesn't
            # double-penalise.
            rewards.append(1.0)
            continue
        well_formed = sum(1 for tc in log.tool_calls if tc.tool_name in KNOWN_TOOL_NAMES)
        rewards.append(well_formed / total)
    return rewards


# ===========================================================================
# r_schema_valid -- arguments the DB would actually accept.
# ===========================================================================
def _valid_kg_traverse_args(args: dict[str, Any]) -> bool:
    edge_types = args.get("edge_types")
    if edge_types and not all(e in EDGE_TYPES for e in edge_types):
        return False
    if not (1 <= args.get("max_hops", 3) <= MAX_HOPS_CEILING):
        return False
    if not (1 <= args.get("max_nodes", 500) <= MAX_NODES_CEILING):
        return False
    return args.get("direction", "out") in VALID_DIRECTIONS


def _valid_kg_search_args(args: dict[str, Any]) -> bool:
    node_types = args.get("node_types")
    if node_types and not all(n in NODE_TYPES for n in node_types):
        return False
    if not (1 <= args.get("k", 20) <= MAX_SEARCH_K):
        return False
    return 0 <= args.get("expand_hops", 2) <= MAX_HOPS_CEILING


def _valid_kg_propose_item(item: dict[str, Any]) -> bool:
    op = item.get("op")
    if op is None:
        return False
    if op.endswith("node") and item.get("node_type") not in NODE_TYPES:
        return False
    if op.endswith("edge") and item.get("edge_type") not in EDGE_TYPES:
        return False
    conf = item.get("agent_confidence")
    return conf is None or (0.0 <= conf <= 1.0)


def _valid_kg_propose_args(args: dict[str, Any]) -> bool:
    items = args.get("items") or []
    return all(_valid_kg_propose_item(item) for item in items)


# Per-tool value validators. Tools absent from this mapping (including
# every unknown/other tool name) have no additional value constraints
# defined here and are treated as valid -- `r_format` is what penalises an
# unknown tool NAME; this dispatch only judges the ARGUMENTS of a tool this
# module already recognises.
_VALIDATORS: dict[str, Any] = {
    "kg_traverse": _valid_kg_traverse_args,
    "kg_dependency_closure": lambda args: 1 <= args.get("max_hops", 4) <= MAX_HOPS_CEILING,
    "kg_impact_radius": lambda args: 1 <= args.get("max_hops", 4) <= MAX_HOPS_CEILING,
    "kg_search": _valid_kg_search_args,
    "kg_lexical_search": lambda args: 1 <= args.get("k", 10) <= MAX_LEXICAL_K,
    "kg_propose": _valid_kg_propose_args,
}


def validate_tool_call_args(tool_name: str, args: dict[str, Any]) -> bool:
    validator = _VALIDATORS.get(tool_name)
    return validator(args) if validator is not None else True


def r_schema_valid(completions: list[Any], **kwargs: Any) -> list[float]:
    rewards = []
    for log in episode_logs(completions, kwargs):
        if log.tool_call_count == 0:
            rewards.append(1.0)
            continue
        valid = sum(
            1
            for tc in log.tool_calls
            if tc.tool_name in KNOWN_TOOL_NAMES
            and validate_tool_call_args(tc.tool_name, tc.arguments)
        )
        rewards.append(valid / log.tool_call_count)
    return rewards
