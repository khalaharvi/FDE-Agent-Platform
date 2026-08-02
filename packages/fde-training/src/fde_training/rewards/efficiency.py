"""rewards/efficiency.py -- process shaping: retrieve enough, but not too
much, and don't burn an unreasonable amount of cost doing it.

`r_hop_efficiency` is deliberately TWO-SIDED -- it penalises under-retrieval
(0 calls, or fewer than the query's own `hops_required` signal implies) AND
over-retrieval (redundant/excessive calls). This must never collapse to a
one-sided "fewer calls = better" shape. Search-R1 / R1-Searcher mask tokens
inside retrieved content (loss masking, not a reward term -- see
`chat_template.jinja`/`sft_config.py`) but also warn, alongside
GraphRAG-R1's Progressive Retrieval Attenuation, that a pure outcome reward
collapses retrieval depth over training ("shortcut exploitation" / "local
optimization traps": the documented failure mode where mean tool-call
count DECREASES over GRPO training because the policy discovers it can
sometimes guess the answer for free). `r_hop_efficiency` exists
specifically to counter this, and `monitor.RewardHackingMonitor` exists to
catch it happening anyway if the weighting is wrong.

`r_cost` is GraphRAG-R1's Cost-Aware F1 idea, narrowed to just the cost
half: a small, low-weight, SEPARATE penalty on raw token/DB-time cost so
cost pressure can never dominate the correctness terms (documented
"fortunate hallucination" risk: an agent that is cost-penalised too
heavily learns to skip retrieval and guess plausibly instead, which is the
opposite failure mode `r_hop_efficiency`'s under-retrieval side already
guards against -- keeping `r_cost` separate and lightly weighted is what
keeps these two terms from fighting each other).
"""

from __future__ import annotations

import json
import math
from typing import Any

from fde_training.rewards._episode import EpisodeLog, _kwarg_per_example, episode_logs


# ===========================================================================
# r_hop_efficiency
# ===========================================================================
def r_hop_efficiency(completions: list[Any], **kwargs: Any) -> list[float]:
    rewards = []
    for i, log in enumerate(episode_logs(completions, kwargs)):
        hops_required = _kwarg_per_example(kwargs, "hops_required", i, 2)
        ideal_lo = max(1, hops_required)
        ideal_hi = ideal_lo + 2  # a little headroom: exploration/verification calls are fine
        n = log.tool_call_count

        if n == 0:
            score = 0.0
        elif n < ideal_lo:
            score = n / ideal_lo
        elif n <= ideal_hi:
            score = 1.0
        else:
            score = max(0.0, 1.0 - 0.15 * (n - ideal_hi))

        # Redundancy penalty: exact-duplicate adjacent calls waste budget
        # without adding hops (the over-retrieval half of "over-retrieval"
        # that a raw call-count band alone would miss -- 3 calls that are
        # all the SAME call is worse than 3 genuinely different ones).
        n_dupes = _count_adjacent_duplicate_calls(log)
        score = max(0.0, score - 0.1 * n_dupes)
        rewards.append(score)
    return rewards


def _count_adjacent_duplicate_calls(log: EpisodeLog) -> int:
    count = 0
    for a, b in zip(log.tool_calls, log.tool_calls[1:], strict=False):
        if a.tool_name == b.tool_name and canonical_args(a.arguments) == canonical_args(
            b.arguments
        ):
            count += 1
    return count


def canonical_args(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


# ===========================================================================
# r_cost
# ===========================================================================
_COST_TOKEN_REFERENCE = 4000  # tokens; typical multi-hop episode budget
_COST_DB_MS_REFERENCE = 2000.0  # ms; a handful of kg_traverse calls


def r_cost(completions: list[Any], **kwargs: Any) -> list[float]:
    """Bounded to [-1, 0] BEFORE `compose_rewards` applies its own weight,
    and given the lowest weight in `DEFAULT_WEIGHTS`, so cost pressure can
    nudge the policy toward efficiency but can never dominate correctness/
    grounding -- an agent that answers wrong-but-cheap must always score
    worse than one that answers right-but-slower. Log-scaled so cost
    differences at the margin (1500 vs 1800 tokens) barely move the reward,
    while a 100x-cost outlier does.
    """
    rewards = []
    for log in episode_logs(completions, kwargs):
        tokens = log.total_tokens_estimate()
        db_ms = log.total_db_ms_estimate()
        token_ratio = tokens / _COST_TOKEN_REFERENCE if _COST_TOKEN_REFERENCE else 0.0
        db_ratio = db_ms / _COST_DB_MS_REFERENCE if _COST_DB_MS_REFERENCE else 0.0
        cost_signal = 0.5 * token_ratio + 0.5 * db_ratio
        # log1p keeps small overages nearly free and only bites hard on
        # genuine outliers; clipped to [-1, 0].
        penalty = -min(1.0, math.log1p(max(0.0, cost_signal)) / math.log1p(3.0))
        rewards.append(penalty)
    return rewards
