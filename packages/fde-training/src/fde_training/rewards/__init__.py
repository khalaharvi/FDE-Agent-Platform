"""fde_training.rewards -- the GRPO reward functions the RL policy is
actually optimised against, plus a hacking monitor.

This package is the heart of the RL stage precisely because it is the part
that is hardest to get right and easiest to get wrong in a way that trains
a WORSE agent while the loss curve looks great. Every term is designed
against one of the VERIFIED GROUNDING facts this pipeline was built from --
see each submodule's own docstring for which failure mode it targets and
which paper/observation backs the design:

  - `validity.py` (`r_format`, `r_schema_valid`) -- turn-level structural
    correctness (KG-R1, 2509.26383).
  - `grounding.py` (`r_grounded`, `r_citation`) -- the anti-hallucination
    axis; `r_grounded` is the highest-weighted term below.
  - `outcome.py` (`r_outcome`, `r_retrieval_quality`) -- global task
    success and nDCG-based retrieval quality (KG-R1's "global" half of its
    turn-level/global split).
  - `efficiency.py` (`r_hop_efficiency`, `r_cost`) -- process shaping
    against the documented GRPO search-agent collapse (Search-R1 /
    R1-Searcher / GraphRAG-R1's Progressive Retrieval Attenuation) and
    against overthinking (GraphRAG-R1's Cost-Aware F1).
  - `monitor.py` (`RewardHackingMonitor`) -- watches training for that
    collapse happening anyway.
  - `_episode.py` -- the shared transcript parser every term above builds
    on (`EpisodeLog`).

This module is the single source of truth for `DEFAULT_WEIGHTS` and for
`compose_rewards`, and re-exports every public name the previous
single-file `grpo_rewards.py` exposed, so
`from fde_training.rewards import r_grounded, DEFAULT_WEIGHTS` keeps
working across the split.

TRL / verl compatibility, and the "no heavy imports" contract, are
documented once in `_episode.py`'s module docstring rather than repeated in
every submodule.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fde_training.rewards._episode import CITATION_RE, EpisodeLog, ToolCallRecord
from fde_training.rewards.efficiency import canonical_args, r_cost, r_hop_efficiency
from fde_training.rewards.grounding import r_citation, r_grounded
from fde_training.rewards.monitor import (
    RewardHackingCollapseWarning,
    RewardHackingMonitor,
    StepStats,
)
from fde_training.rewards.outcome import r_outcome, r_retrieval_quality
from fde_training.rewards.validity import (
    EDGE_TYPES,
    KNOWN_TOOL_NAMES,
    MAX_HOPS_CEILING,
    MAX_LEXICAL_K,
    MAX_NODES_CEILING,
    MAX_SEARCH_K,
    NODE_TYPES,
    VALID_DIRECTIONS,
    r_format,
    r_schema_valid,
)

__all__ = [
    "CITATION_RE",
    "DEFAULT_WEIGHTS",
    "EDGE_TYPES",
    "KNOWN_TOOL_NAMES",
    "MAX_HOPS_CEILING",
    "MAX_LEXICAL_K",
    "MAX_NODES_CEILING",
    "MAX_SEARCH_K",
    "NODE_TYPES",
    "REWARD_FUNCTIONS",
    "VALID_DIRECTIONS",
    "EpisodeLog",
    "RewardHackingCollapseWarning",
    "RewardHackingMonitor",
    "StepStats",
    "ToolCallRecord",
    "canonical_args",
    "compose_rewards",
    "r_citation",
    "r_cost",
    "r_format",
    "r_grounded",
    "r_hop_efficiency",
    "r_outcome",
    "r_retrieval_quality",
    "r_schema_valid",
]

# ===========================================================================
# DEFAULT_WEIGHTS -- the single source of truth for how the eight terms
# above combine into one scalar GRPO reward. Every weight is a deliberate
# priority ordering, not a tuned hyperparameter picked by grid search:
# ===========================================================================
DEFAULT_WEIGHTS: dict[str, float] = {
    # Anti-hallucination is the single highest priority for a system whose
    # entire value proposition is "agents propose, humans dispose" against a
    # graph that must be trustworthy -- see db/004_hitl_gates.sql. A model
    # that hallucinates confidently is worse than one that is inefficient.
    "r_grounded": 0.30,
    # Task success is the other primary axis; weighted equal to grounding
    # minus a small margin so grounding wins ties (a correct-but-ungrounded
    # answer -- e.g. right by luck -- must not outscore an honest partial
    # answer that is fully grounded).
    "r_outcome": 0.25,
    # Structural correctness gates -- without valid schema/args the episode
    # cannot even reach the DB, so these matter, but they are necessary-not-
    # sufficient conditions and are weighted well below the two above.
    "r_schema_valid": 0.10,
    "r_format": 0.05,
    # Process shaping: counter the documented "tool-call count decreasing
    # over training" collapse without letting hop-count micromanagement
    # outweigh whether the answer was right and grounded.
    "r_hop_efficiency": 0.10,
    "r_retrieval_quality": 0.10,
    "r_citation": 0.05,
    # Cost is real but must never dominate -- see efficiency.r_cost's own
    # docstring.
    "r_cost": 0.05,
}

REWARD_FUNCTIONS: dict[str, Callable[..., list[float]]] = {
    "r_format": r_format,
    "r_schema_valid": r_schema_valid,
    "r_grounded": r_grounded,
    "r_outcome": r_outcome,
    "r_hop_efficiency": r_hop_efficiency,
    "r_retrieval_quality": r_retrieval_quality,
    "r_citation": r_citation,
    "r_cost": r_cost,
}


def compose_rewards(
    completions: list[Any],
    weights: dict[str, float] | None = None,
    **kwargs: Any,
) -> tuple[list[float], dict[str, list[float]]]:
    """Run every reward function, apply `weights` (defaults to
    `DEFAULT_WEIGHTS`), and return (combined_reward_per_example,
    per_term_breakdown) -- the breakdown is what `RewardHackingMonitor`
    consumes and what should be logged to your experiment tracker per step;
    never log only the combined scalar, or a collapsing term (e.g.
    `r_hop_efficiency` quietly trending down) is invisible until it has
    already damaged the policy.
    """
    weights = weights or DEFAULT_WEIGHTS
    per_term: dict[str, list[float]] = {}
    n = len(completions)
    combined = [0.0] * n
    for name, fn in REWARD_FUNCTIONS.items():
        w = weights.get(name, 0.0)
        values = fn(completions, **kwargs)
        if len(values) != n:
            msg = f"reward fn {name} returned {len(values)} values for {n} completions"
            raise ValueError(msg)
        per_term[name] = values
        for i, v in enumerate(values):
            combined[i] += w * v
    return combined, per_term
