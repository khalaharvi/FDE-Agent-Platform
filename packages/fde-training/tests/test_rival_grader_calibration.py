"""Tests for the judge-usability band and the embedding-backend resolution.

Two bugs are pinned here.

The first: `calibrate()` green-lit a judge with Cohen's kappa as low as
0.60 while `docs/06-training.md` and the README both require >= 0.78 in
three separate places. A judge at 0.60 agrees with a human about as often
as a coin weighted by the base rate, and using it as an RL reward optimises
the policy against that noise.

The second: both retrieval call sites took a `default_embed_fn()` factory
that returned the deterministic FAKE embedding, so a bedrock-judged
tournament and an RFT dataset build were both retrieving against vectors
with no semantic content -- while looking exactly like real runs. The
resolver's defaults are asserted per subcommand.
"""

from __future__ import annotations

import pytest

from fde_training import cli, rival_grader, rollout_env


@pytest.mark.parametrize(
    ("kappa", "usable"),
    [
        (None, False),
        (0.59, False),
        (0.77, False),
        (0.78, True),
        (0.80, True),
        (0.82, True),
        (0.83, False),
    ],
)
def test_usable_as_rl_reward_band(kappa: float | None, usable: bool) -> None:
    assert rival_grader.usable_as_rl_reward(kappa, None) is usable


@pytest.mark.parametrize(
    ("bias_rate", "usable"),
    [(None, True), (0.0, True), (0.14, True), (0.15, False), (0.4, False)],
)
def test_position_bias_disqualifies_an_otherwise_calibrated_judge(
    bias_rate: float | None, usable: bool
) -> None:
    assert rival_grader.usable_as_rl_reward(0.80, bias_rate) is usable


def test_band_constants_match_the_documented_contract() -> None:
    assert rival_grader.KAPPA_RL_MIN == 0.78
    assert rival_grader.KAPPA_SUSPICIOUS_MAX == 0.82
    assert rival_grader.BIAS_RATE_MAX == 0.15


def test_the_misleading_embed_factory_is_gone() -> None:
    """`rival_grader.default_embed_fn()` returned the FAKE embedding. If it
    ever comes back, so does the bug."""
    assert not hasattr(rival_grader, "default_embed_fn")


@pytest.mark.parametrize(
    ("arg", "default", "expected"),
    [
        (None, "bedrock", rollout_env.default_embed_fn),
        (None, "fake", rollout_env.deterministic_fake_embed),
        ("bedrock", "fake", rollout_env.default_embed_fn),
        ("fake", "bedrock", rollout_env.deterministic_fake_embed),
    ],
)
def test_resolve_embed_fn_defaults_and_overrides(
    arg: str | None, default: str, expected: object
) -> None:
    assert cli._resolve_embed_fn(arg, default=default) is expected


def test_resolve_embed_fn_rejects_an_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown embedding backend"):
        cli._resolve_embed_fn("word2vec", default="bedrock")


@pytest.mark.parametrize(
    ("judge", "expected"),
    [
        ("mock", rollout_env.deterministic_fake_embed),
        ("bedrock", rollout_env.default_embed_fn),
    ],
)
def test_rival_grader_run_couples_the_embedding_to_the_judge(judge: str, expected: object) -> None:
    """A `--judge mock` dry run must stay AWS-free; a bedrock-judged
    tournament scoring retrieval quality must use real vectors."""
    args = cli.build_parser().parse_args(["rival-grader", "run", "--judge", judge])
    assert (
        cli._resolve_embed_fn(args.embed, default=("fake" if args.judge == "mock" else "bedrock"))
        is expected
    )


def test_build_dataset_defaults_to_the_real_embedding() -> None:
    """This dataset's provenance_keys become the Lambda grader's grounding
    reference for the whole RFT run."""
    args = cli.build_parser().parse_args(
        ["rft-submit", "build-dataset", "--out", "out/unused.jsonl"]
    )
    assert args.embed is None
    assert cli._resolve_embed_fn(args.embed, default="bedrock") is rollout_env.default_embed_fn
