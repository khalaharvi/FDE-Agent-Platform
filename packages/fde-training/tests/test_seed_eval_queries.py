"""Tests for `fde_training.seed_eval` and the committed question fixture.

`trn.eval_query` is the head of three pipelines (`rival-grader run`,
`generate-traces`, `rft-submit build-dataset`) and on a fresh database it
is empty, so all three run to completion and do nothing. The fixture and
this module are the fix; these tests keep the fixture parseable and the
seeding idempotent.

The `generate-traces` test asserts a BEHAVIOUR, not a count: at least one
acceptance and at least one rejection over a seeded graph. `docs/06`'s old
"kept 4 of 16" was numerology -- ANN tie order under `relaxed_order` is not
stable across environments, so a pinned count is a test that fails for
reasons that have nothing to do with rejection sampling working.
"""

from __future__ import annotations

import json
import random
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from fde_training import generate_traces, rollout_env, seed_eval


def test_committed_fixture_parses_and_covers_the_intended_spread() -> None:
    rows = seed_eval.load_eval_query_file(seed_eval.DEFAULT_FIXTURE)
    assert len(rows) == 16

    splits: dict[str, int] = {}
    difficulties: dict[str, int] = {}
    for row in rows:
        splits[row["split"]] = splits.get(row["split"], 0) + 1
        difficulties[row["difficulty"]] = difficulties.get(row["difficulty"], 0) + 1
    assert splits == {"train": 10, "validation": 2, "test": 2, "holdout": 2}
    assert difficulties == {"easy": 6, "medium": 6, "hard": 4}
    assert all(row["relevant_keys"] for row in rows)
    assert all(1 <= row["hops_required"] <= 3 for row in rows)


def test_fixture_keys_are_all_from_the_quote_to_cash_scenario() -> None:
    """A typo'd gold key does not make a question slightly harder, it makes
    it unanswerable -- and drags every retriever variant's score down
    equally, so the tournament still looks healthy."""
    known = {
        "proc.quote_to_cash",
        "act.create_quote",
        "act.discount_review",
        "ctl.discount_threshold_20",
        "sys.cpq",
        "role.deal_desk",
    }
    for row in seed_eval.load_eval_query_file(seed_eval.DEFAULT_FIXTURE):
        assert set(row["relevant_keys"]) <= known, row["question"]


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"difficulty": "trivial"}, "difficulty"),
        ({"split": "train_set"}, "split"),
        ({"hops_required": 0}, "hops_required"),
        ({"relevant_keys": []}, "relevant_keys"),
        ({"question": "   "}, "question"),
    ],
)
def test_invalid_rows_are_rejected_with_the_line_number(
    tmp_path: Any, mutation: dict[str, Any], match: str
) -> None:
    row = {
        "question": "Which control gates quote creation?",
        "relevant_keys": ["ctl.discount_threshold_20"],
        "difficulty": "easy",
        "hops_required": 1,
        "split": "train",
    }
    row.update(mutation)
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match=match) as excinfo:
        seed_eval.load_eval_query_file(path)
    assert ":1:" in str(excinfo.value)


def test_missing_field_is_named(tmp_path: Any) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"question": "q", "relevant_keys": ["a.b"]}\n')
    with pytest.raises(ValueError, match=r"missing required field"):
        seed_eval.load_eval_query_file(path)


@pytest.fixture
def fresh_engagement_id() -> str:
    """`trn.eval_query.engagement_id` is a bare uuid column with no foreign
    key into `kg`, so a seeding test needs an id, not a graph -- and giving
    each its own keeps one test's leftover rows out of the next one's
    counts."""
    return str(uuid.uuid4())


@pytest.mark.requires_db
def test_reseeding_with_replace_reconciles_in_place(fresh_engagement_id: str, db_dsn: str) -> None:
    """Re-seeding must not duplicate the set, and must not discard the
    `query_id` every judged duel and pooled relevance key hangs off."""
    engagement_id = fresh_engagement_id
    rows = seed_eval.load_eval_query_file(seed_eval.DEFAULT_FIXTURE)

    assert seed_eval.seed_eval_queries(engagement_id, rows) == 16
    assert _eval_query_count(db_dsn, engagement_id) == 16
    first_ids = _eval_query_ids(db_dsn, engagement_id)

    seed_eval.seed_eval_queries(engagement_id, rows, replace=True)
    assert _eval_query_count(db_dsn, engagement_id) == 16
    assert _eval_query_ids(db_dsn, engagement_id) == first_ids

    # Without --replace the command stays purely additive.
    seed_eval.seed_eval_queries(engagement_id, rows)
    assert _eval_query_count(db_dsn, engagement_id) == 32


@pytest.mark.requires_db
def test_replace_updates_a_changed_gold_key_set(fresh_engagement_id: str, db_dsn: str) -> None:
    engagement_id = fresh_engagement_id
    rows = seed_eval.load_eval_query_file(seed_eval.DEFAULT_FIXTURE)
    seed_eval.seed_eval_queries(engagement_id, rows, replace=True)

    edited = [dict(row) for row in rows]
    edited[0]["relevant_keys"] = ["sys.cpq"]
    edited[0]["split"] = "holdout"
    seed_eval.seed_eval_queries(engagement_id, edited, replace=True)

    with psycopg.connect(db_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT relevant_keys, split FROM trn.eval_query
                WHERE engagement_id = %(eng)s::uuid AND question = %(q)s""",
            {"eng": engagement_id, "q": edited[0]["question"]},
        )
        row = cur.fetchone()
    assert row is not None
    assert row["relevant_keys"] == ["sys.cpq"]
    assert row["split"] == "holdout"


@pytest.mark.requires_db
def test_seeded_queries_load_as_generate_traces_seed_questions(
    fresh_engagement_id: str,
) -> None:
    engagement_id = fresh_engagement_id
    seed_eval.seed_eval_queries(
        engagement_id, seed_eval.load_eval_query_file(seed_eval.DEFAULT_FIXTURE), replace=True
    )
    seeds = generate_traces.load_seeds_from_eval_query(engagement_id)
    assert len(seeds) == 16
    assert all(s.gold_keys for s in seeds)
    assert all(s.engagement_id == engagement_id for s in seeds)


@pytest.mark.requires_db
def test_rejection_sampling_both_accepts_and_rejects(seeded_graph: dict[str, Any]) -> None:
    """The heuristic teacher is deliberately imperfect (it sometimes
    fabricates a citation, sometimes calls kg_search above the k ceiling),
    so a run that accepts everything or rejects everything means the gates
    are not actually gating.

    The gold key is read off the live retrieval rather than hardcoded: what
    the ANN returns first depends on the environment's pgvector build, and
    pinning it would make this a test of that instead of of the gates.
    """
    engagement_id = seeded_graph["engagement_id"]
    question = "Which control gates quote creation?"

    probe = rollout_env.RolloutEnv(
        engagement_id=engagement_id,
        embed_fn=rollout_env.deterministic_fake_embed,
        dsn=seeded_graph["dsn"],
    )
    probe.reset(task_kind="probe", task_input={"question": question})
    top_key = probe.kg_search(query=question, k=5)["results"][0]["node_key"]

    seed = generate_traces.SeedQuestion(
        engagement_id=engagement_id, question=question, gold_keys=[top_key]
    )
    stats = generate_traces.generate(
        [seed],
        n_samples_per_question=12,
        teacher_fn=generate_traces.heuristic_teacher_fn(random.Random(0)),  # noqa: S311
        embed_fn=rollout_env.deterministic_fake_embed,
    )

    assert stats.attempted == 12
    assert stats.accepted >= 1, "no trajectory was accepted -- the gates reject everything"
    rejected = (
        stats.rejected_ungrounded
        + stats.rejected_schema_invalid
        + stats.rejected_wrong_answer
        + stats.rejected_other
    )
    assert rejected >= 1, "nothing was rejected -- the STaR gates are not gating"
    assert stats.accepted + rejected == stats.attempted
    assert len(stats.accepted_session_ids) == stats.accepted


def _eval_query_count(dsn: str, engagement_id: str) -> int:
    return len(_eval_query_ids(dsn, engagement_id))


def _eval_query_ids(dsn: str, engagement_id: str) -> list[int]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT query_id FROM trn.eval_query
                WHERE engagement_id = %(eng)s::uuid ORDER BY query_id""",
            {"eng": engagement_id},
        )
        return [r["query_id"] for r in cur.fetchall()]


def test_default_fixture_path_points_at_a_real_file() -> None:
    """The CLI's `--file` default; a broken path would surface as a
    confusing FileNotFoundError from inside the seeding command."""
    assert seed_eval.DEFAULT_FIXTURE.is_file()
    assert seed_eval.DEFAULT_FIXTURE.name == "eval_queries.jsonl"
