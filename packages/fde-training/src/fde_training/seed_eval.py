"""seed_eval.py -- load `trn.eval_query` from a committed question file.

Why this exists
---------------
`trn.eval_query` is the head of three pipelines: `rival-grader run` duels
retriever variants over it, `generate-traces` uses it as STaR seed
questions, and `rft-submit build-dataset` turns it into the Bedrock RFT
prompt set. On a freshly migrated database that table is empty, so all
three commands run to completion, report success, and do nothing -- the
worst failure shape available, because it looks exactly like a working
pipeline with no data yet. The only insert anywhere in the repo lived
inside `db/tests/smoke_test.sql`'s rolled-back transaction.

The fixture ships questions WITHOUT an `engagement_id`
------------------------------------------------------
`fixtures/eval_queries.jsonl` names graph keys (`proc.quote_to_cash`,
`ctl.discount_threshold_20`, ...) but not an engagement, and the CLI
supplies the engagement at seed time. Every engagement gets a fresh uuid,
so an engagement id baked into a committed file would be wrong for every
database except the one it was exported from -- and wrong in the silent
way, since a mismatched uuid seeds rows that simply never match a query.

Validation is strict on purpose: `relevant_keys` are the gold set the
outcome reward is scored against, so a typo'd key does not produce a
slightly worse metric, it produces a question that is unanswerable by
construction and drags every variant's score down equally.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fde_mcp.logging import get_logger
from fde_training import common

log = get_logger(__name__)

VALID_DIFFICULTIES = frozenset({"easy", "medium", "hard"})
# `trn.split` (db/009_training.sql). `holdout` exists so a slice of the
# eval set can be kept out of every tuning loop, not just out of training.
VALID_SPLITS = frozenset({"train", "validation", "test", "holdout"})

REQUIRED_FIELDS = ("question", "relevant_keys", "difficulty", "hops_required", "split")

# Repo-relative, not packaged into the wheel: this is an operator fixture
# for seeding a development/demo graph, not library data. A deployment that
# seeds from its own curated question set passes `--file` and never touches
# this path.
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
DEFAULT_FIXTURE = FIXTURES_DIR / "eval_queries.jsonl"


def load_eval_query_file(path: str | Path) -> list[dict[str, Any]]:
    """Parse and validate an eval-query JSONL file.

    Args:
        path: JSONL file, one query object per line. Each object needs
            `question`, `relevant_keys` (non-empty list of node keys),
            `difficulty` in `VALID_DIFFICULTIES`, `hops_required` (positive
            int), and `split` in `VALID_SPLITS`.

    Returns:
        The parsed rows, in file order.

    Raises:
        ValueError: on the first malformed row, naming the line number --
            a partially-seeded eval set is worse than an unseeded one,
            because the resulting metrics look plausible.
    """
    rows: list[dict[str, Any]] = []
    with Path(path).open() as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                msg = f"{path}:{lineno}: not valid JSON: {exc}"
                raise ValueError(msg) from exc
            _validate_row(row, f"{path}:{lineno}")
            rows.append(row)
    if not rows:
        msg = f"{path}: no rows"
        raise ValueError(msg)
    return rows


def _validate_row(row: Any, where: str) -> None:
    if not isinstance(row, dict):
        msg = f"{where}: expected a JSON object, got {type(row).__name__}"
        raise ValueError(msg)
    missing = [k for k in REQUIRED_FIELDS if k not in row]
    if missing:
        msg = f"{where}: missing required field(s) {missing}"
        raise ValueError(msg)
    if not isinstance(row["question"], str) or not row["question"].strip():
        msg = f"{where}: question must be a non-empty string"
        raise ValueError(msg)
    keys = row["relevant_keys"]
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k for k in keys):
        msg = f"{where}: relevant_keys must be a non-empty list of node keys"
        raise ValueError(msg)
    if row["difficulty"] not in VALID_DIFFICULTIES:
        msg = f"{where}: difficulty {row['difficulty']!r} not in {sorted(VALID_DIFFICULTIES)}"
        raise ValueError(msg)
    if row["split"] not in VALID_SPLITS:
        msg = f"{where}: split {row['split']!r} not in {sorted(VALID_SPLITS)}"
        raise ValueError(msg)
    hops = row["hops_required"]
    if not isinstance(hops, int) or isinstance(hops, bool) or hops < 1:
        msg = f"{where}: hops_required must be a positive integer, got {hops!r}"
        raise ValueError(msg)


def seed_eval_queries(
    engagement_id: str, rows: list[dict[str, Any]], *, replace: bool = False
) -> int:
    """Load `rows` into `trn.eval_query` for one engagement.

    Runs as the `fde_training` role, which holds INSERT/SELECT/UPDATE on
    this table and deliberately NOT DELETE
    (db/010_roles_and_seed_policy.sql:79).

    That grant is why `replace` updates rather than deletes, and the grant
    is right: `trn.duel` references `query_id` and
    `trn.eval_query.pooled_keys` accumulates TREC-style pooled relevance
    judgements against it. Deleting a question to re-seed it would throw
    away every duel ever judged for it -- the expensive, human-adjudicated
    part of the eval set -- and would fail on the foreign key anyway the
    moment a tournament had been run. So a re-seed reconciles in place,
    matched on question text, and `query_id` survives.

    Args:
        engagement_id: The engagement these questions are about.
        rows: Validated rows from `load_eval_query_file`.
        replace: Update questions that already exist for this engagement to
            match the file instead of inserting a second copy. Off by
            default, so the plain command is purely additive.

    Returns:
        Number of rows inserted plus updated (i.e. `len(rows)`).
    """
    inserted = 0
    updated = 0
    with common.connect() as db:
        cur = db.cursor()
        existing: dict[str, int] = {}
        if replace:
            cur.execute(
                "SELECT query_id, question FROM trn.eval_query WHERE engagement_id = %(eng)s::uuid",
                {"eng": engagement_id},
            )
            existing = {r["question"]: r["query_id"] for r in cur.fetchall()}

        for row in rows:
            params = {
                "eng": engagement_id,
                "q": row["question"],
                "keys": list(row["relevant_keys"]),
                "diff": row["difficulty"],
                "hops": row["hops_required"],
                "split": row["split"],
            }
            query_id = existing.get(row["question"])
            if query_id is not None:
                cur.execute(
                    """UPDATE trn.eval_query
                          SET relevant_keys = %(keys)s, difficulty = %(diff)s,
                              hops_required = %(hops)s, split = %(split)s
                        WHERE query_id = %(qid)s""",
                    {**params, "qid": query_id},
                )
                updated += 1
            else:
                cur.execute(
                    """INSERT INTO trn.eval_query
                           (engagement_id, question, relevant_keys, difficulty,
                            hops_required, split)
                       VALUES (%(eng)s::uuid, %(q)s, %(keys)s, %(diff)s, %(hops)s, %(split)s)""",
                    params,
                )
                inserted += 1

    log.info(
        "eval_queries_seeded",
        engagement_id=engagement_id,
        inserted=inserted,
        updated=updated,
        reconciled=replace,
    )
    return inserted + updated
