"""rewards/outcome.py -- did the episode actually answer the question, and
did it retrieve well along the way.

`r_outcome` is the task-success axis: F1 of the cited node keys against the
gold set for the eval query (`kwargs["gold_keys"]`, sourced from
`trn.eval_query.relevant_keys`). Weighted just below `r_grounded` in
`DEFAULT_WEIGHTS` -- close, but not equal, and never higher -- so that a
correct-but-ungrounded answer (right by luck, or by pattern-matching a
question it has seen before rather than by reading the graph) cannot
outscore an honest, fully-grounded partial answer. Rewarding lucky
correctness over honest partial evidence is exactly backwards for a system
whose whole point is that a human downstream trusts the citations enough
to act on them.

`r_retrieval_quality` is nDCG@k of the retrieved set against pooled
relevance judgements from `trn.eval_query` (TREC-style pooling -- judge the
union of what every variant has ever returned, once, reuse everywhere,
which avoids "unjudged == irrelevant" bias against a new policy/variant;
see that table's own comment). Keys outside the pool are still scored as
non-relevant (rank 0), which is the standard, explicitly-accepted TREC
convention: pooling controls WHICH items get a human judgement, not how
unjudged items are scored. This term exists ALONGSIDE `r_outcome` rather
than instead of it because outcome alone cannot tell "retrieved the right
things but phrased the final answer/citations poorly" apart from "never
found the right things in the first place" -- the two failure modes need
different fixes (a chat-template/prompting fix vs. a retrieval-config fix),
and only nDCG on the raw retrieved set can distinguish them.
"""

from __future__ import annotations

import math
from typing import Any

from fde_training.rewards._episode import CITATION_RE, EpisodeLog, _kwarg_per_example, episode_logs


def _f1(predicted: set[str], gold: set[str]) -> float:
    if not predicted and not gold:
        return 1.0
    if not predicted or not gold:
        return 0.0
    tp = len(predicted & gold)
    if tp == 0:
        return 0.0
    precision = tp / len(predicted)
    recall = tp / len(gold)
    return 2 * precision * recall / (precision + recall)


def r_outcome(completions: list[Any], **kwargs: Any) -> list[float]:
    """F1 against the gold node-key set for the eval query. Falls back to
    an empty gold set (F1=0 unless the answer also predicts nothing, in
    which case F1 is defined as 1.0 -- correctly predicting "nothing"
    against a genuinely empty gold set) when `gold_keys` is absent from
    `kwargs`.
    """
    rewards = []
    for i, log in enumerate(episode_logs(completions, kwargs)):
        gold = set(_kwarg_per_example(kwargs, "gold_keys", i, []))
        predicted = set(CITATION_RE.findall(log.final_answer or ""))
        rewards.append(_f1(predicted, gold))
    return rewards


def _primary_ranked_list(log: EpisodeLog) -> list[str]:
    """The first kg_search (the RRF-fused primary retrieval tool) call's
    ranked result list, in the order returned (already rank-ordered by
    `kg.hybrid_search`'s `ORDER BY f.score DESC`, see db/008_retrieval.sql).
    Falls back to the union across all calls, unordered, if no kg_search
    call is present -- a strictly worse signal but still usable rather than
    scoring 0 outright for an agent that only used kg_traverse.
    """
    for tc in log.tool_calls:
        if tc.tool_name == "kg_search" and tc.result:
            results = tc.result.get("results") or []
            keys = [r["node_key"] for r in results if isinstance(r, dict) and r.get("node_key")]
            if keys:
                return keys
    return sorted(log.retrieved_keys())


def r_retrieval_quality(completions: list[Any], **kwargs: Any) -> list[float]:
    rewards = []
    for i, log in enumerate(episode_logs(completions, kwargs)):
        relevant = set(_kwarg_per_example(kwargs, "relevant_keys", i, []))
        if not relevant:
            rewards.append(1.0)  # nothing to rank against -- neutral, not a penalty
            continue
        ranked_keys = _primary_ranked_list(log)
        k = len(ranked_keys) or 1
        dcg = sum(
            (1.0 if key in relevant else 0.0) / math.log2(rank + 2)
            for rank, key in enumerate(ranked_keys)
        )
        ideal_hits = min(len(relevant), k)
        idcg = sum(1.0 / math.log2(rank + 2) for rank in range(ideal_hits))
        rewards.append(dcg / idcg if idcg > 0 else 0.0)
    return rewards
