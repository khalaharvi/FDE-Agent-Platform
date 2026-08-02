"""rewards/grounding.py -- the anti-hallucination reward terms.

`r_grounded` is THE anti-hallucination term, and deliberately the
highest-weighted term in `DEFAULT_WEIGHTS` (see `rewards/__init__.py`).
Every entity cited in the final answer must appear in the union of
retrieved provenance across the whole episode. A fluent, well-cited,
format-perfect answer that cites a `node_key` nobody ever retrieved is a
worse outcome than an honest "I don't have enough information" for a
system whose entire value proposition is "agents propose, humans dispose"
against a graph that must be trustworthy (see `db/004_hitl_gates.sql`) --
the reward function must reflect that ordering, not just task success.

`r_citation` is the complementary FORMAT term: does a substantive
declarative sentence carry a citation at all, independent of whether that
citation is correct. Kept as a separate, lower-weighted term from
`r_grounded` on purpose -- a model that learns "always attach some bracketed
token" without learning "that token must be real" would max out r_citation
while r_grounded still catches it, which is exactly the point of scoring
format and correctness independently rather than folding "well-formatted
AND correct" into one term a model could satisfy by degrading either half.
"""

from __future__ import annotations

import re
from typing import Any

from fde_training.rewards._episode import CITATION_RE, episode_logs

_ABSTENTION_PHRASES = (
    "no information found",
    "not enough evidence",
    "cannot find",
    "no results",
    "insufficient evidence",
    "i don't have enough information",
    "no matching",
)


def _is_abstention(lowered_text: str) -> bool:
    return any(p in lowered_text for p in _ABSTENTION_PHRASES)


def r_grounded(completions: list[Any], **kwargs: Any) -> list[float]:
    """Fraction of citations in the final answer that appear in the
    episode's own retrieved provenance.

    An answer with substantive content but zero citations is exactly as
    ungrounded as a wrong citation -- score 0 unless there is genuinely no
    content to ground (empty/None answer, e.g. an intermediate reward call
    mid-episode) or a legitimate "insufficient evidence" abstention (no
    citation needed because no claim is made).
    """
    rewards = []
    for log in episode_logs(completions, kwargs):
        answer = log.final_answer or ""
        cited = set(CITATION_RE.findall(answer))
        if not cited:
            stripped = answer.strip().lower()
            if not stripped or _is_abstention(stripped):
                rewards.append(1.0)
            else:
                rewards.append(0.0)
            continue
        retrieved = log.retrieved_keys()
        grounded_count = sum(1 for k in cited if k in retrieved)
        rewards.append(grounded_count / len(cited))
    return rewards


def r_citation(completions: list[Any], **kwargs: Any) -> list[float]:
    """Fraction of substantive sentences (more than 3 words, not an
    abstention) that carry at least one `[node_key]` citation. Format/
    presence only -- correctness of the citation is `r_grounded`'s job.
    """
    rewards = []
    for log in episode_logs(completions, kwargs):
        answer = (log.final_answer or "").strip()
        if not answer:
            rewards.append(1.0)
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer) if s.strip()]
        substantive = [s for s in sentences if len(s.split()) > 3 and not _is_abstention(s.lower())]
        if not substantive:
            rewards.append(1.0)
            continue
        cited = sum(1 for s in substantive if CITATION_RE.search(s))
        rewards.append(cited / len(substantive))
    return rewards
