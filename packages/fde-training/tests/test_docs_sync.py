"""Keep the prose and the constants in agreement.

`docs/06-training.md` says of its reward-weight table: "Source of truth is
`DEFAULT_WEIGHTS`; the weights sum to 1.0 and this table is checked against
it in CI." No such check existed, and the same document's kappa band
disagreed with `rival_grader.calibrate()` by 0.18 for as long as both
existed. Documentation that claims to be verified and is not is worse than
documentation that admits it is prose, because a reader stops checking.

These tests parse the documents. They are intentionally forgiving about
formatting and strict about numbers: reflowing a table is not a
regression, changing a weight in one place and not the other is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fde_training import rival_grader
from fde_training.rewards import DEFAULT_WEIGHTS

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_TRAINING = REPO_ROOT / "docs" / "06-training.md"
README = REPO_ROOT / "README.md"

# | `r_grounded` | 0.30 | every claim appears in a retrieved provenance record |
_WEIGHT_ROW = re.compile(r"^\|\s*`(r_[a-z_]+)`\s*\|\s*([0-9.]+)\s*\|", re.MULTILINE)


def test_default_weights_sum_to_one() -> None:
    """`docs/11-python-conventions.md` §10: a new reward term rebalances the
    weights, it does not append to them."""
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)


def test_docs_reward_table_matches_default_weights() -> None:
    text = DOCS_TRAINING.read_text()
    documented = {name: float(weight) for name, weight in _WEIGHT_ROW.findall(text)}
    assert documented, f"no reward-weight table rows found in {DOCS_TRAINING}"
    assert documented == pytest.approx(dict(DEFAULT_WEIGHTS))


def test_docs_and_readme_state_the_same_kappa_band_as_the_code() -> None:
    """Three documents and one predicate have to agree on the number that
    decides whether a judge may be used as an RL reward at all."""
    docs = DOCS_TRAINING.read_text()
    readme = README.read_text()
    minimum = str(rival_grader.KAPPA_RL_MIN)
    suspicious = str(rival_grader.KAPPA_SUSPICIOUS_MAX)

    assert minimum in docs, f"docs/06 does not mention the kappa floor {minimum}"
    assert suspicious in docs, f"docs/06 does not mention the suspicious-high bound {suspicious}"
    assert minimum in readme, f"README does not mention the kappa floor {minimum}"


def test_the_kappa_band_is_a_band() -> None:
    assert rival_grader.KAPPA_RL_MIN < rival_grader.KAPPA_SUSPICIOUS_MAX
    assert rival_grader.usable_as_rl_reward(rival_grader.KAPPA_RL_MIN, 0.0)
    assert rival_grader.usable_as_rl_reward(rival_grader.KAPPA_SUSPICIOUS_MAX, 0.0)
    assert not rival_grader.usable_as_rl_reward(rival_grader.KAPPA_RL_MIN - 0.01, 0.0)
    assert not rival_grader.usable_as_rl_reward(rival_grader.KAPPA_SUSPICIOUS_MAX + 0.01, 0.0)
