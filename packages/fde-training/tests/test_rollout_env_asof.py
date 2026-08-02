"""`RolloutEnv`'s commit pinning, end to end against Postgres.

`rollout_env.py`'s module docstring has always claimed that every as-of-
capable retrieval is pinned to the episode's commit. Two of its eight tools
did not honour that: `kg.dependency_closure`/`kg.impact_radius` shipped as
3-arg wrappers that called `kg.traverse` WITHOUT `p_as_of`, so they read
`now()` and an episode replayed after a merge saw a different graph than
the one it was scored against. Nothing detected it -- the queries returned
plausible rows either way.

db/015_closure_asof.sql adds the 4-arg overloads and `rollout_env` calls
them. This test is the end-to-end proof, and it is deliberately written the
way the failure would actually happen: retire an edge in a LATER commit and
check that an episode pinned to the earlier one still sees the path.

`db/tests/smoke_test.sql`'s TEST 25 asserts the same property at the SQL
level. This one asserts that the Python actually passes the parameter --
which is the half that was broken.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from fde_training import rollout_env

pytestmark = pytest.mark.requires_db


@pytest.fixture
def two_commit_graph(db_dsn: str) -> dict[str, Any]:
    """A graph whose second commit retires the dependency the first added.

    Explicit valid times rather than real elapsed time: `kg.close_edge`
    stamps `valid_to`, and an edge closed at the same instant it was opened
    leaves no interval to query as-of.
    """
    engagement_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    t1 = now - timedelta(hours=2)

    with (
        psycopg.connect(db_dsn, autocommit=True, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            """INSERT INTO kg.commit (status, engagement_id, title, authored_by, sealed_by,
                                      sealed_at, content_digest)
               VALUES ('sealed', %(eng)s, 'asof: initial mapping', 'pytest', 'pytest', %(t1)s, 'd1')
               RETURNING commit_id""",
            {"eng": engagement_id, "t1": t1},
        )
        row = cur.fetchone()
        assert row is not None
        commit_one = row["commit_id"]

        cur.execute(
            """INSERT INTO kg.node (engagement_id, node_key, node_type, label, valid_from, commit_id)
               VALUES (%(eng)s, 'act.settle', 'activity', 'Settle Invoice', %(t1)s, %(cid)s),
                      (%(eng)s, 'sys.ledger', 'system',   'General Ledger', %(t1)s, %(cid)s)""",
            {"eng": engagement_id, "t1": t1, "cid": commit_one},
        )
        cur.execute(
            """INSERT INTO kg.edge (engagement_id, edge_key, src_key, dst_key, edge_type,
                                    valid_from, commit_id, human_confirmed)
               VALUES (%(eng)s, kg.make_edge_key('act.settle','depends_on','sys.ledger'),
                       'act.settle', 'sys.ledger', 'depends_on', %(t1)s, %(cid)s, true)""",
            {"eng": engagement_id, "t1": t1, "cid": commit_one},
        )

    return {"engagement_id": engagement_id, "dsn": db_dsn, "commit_one": commit_one}


def _retire_the_dependency(graph: dict[str, Any]) -> None:
    """Second sealed commit, and the edge closed as of its seal."""
    t2 = datetime.now(UTC) - timedelta(hours=1)
    with (
        psycopg.connect(graph["dsn"], autocommit=True, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            """INSERT INTO kg.commit (parent_id, status, engagement_id, title, authored_by,
                                      sealed_by, sealed_at, content_digest)
               VALUES (%(parent)s, 'sealed', %(eng)s, 'asof: retire the dependency', 'pytest',
                       'pytest', %(t2)s, 'd2')""",
            {"parent": graph["commit_one"], "eng": graph["engagement_id"], "t2": t2},
        )
        cur.execute(
            """SELECT kg.close_edge(%(eng)s::uuid,
                       kg.make_edge_key('act.settle','depends_on','sys.ledger'), %(t2)s)""",
            {"eng": graph["engagement_id"], "t2": t2},
        )


def _env(graph: dict[str, Any]) -> rollout_env.RolloutEnv:
    env = rollout_env.RolloutEnv(
        engagement_id=graph["engagement_id"],
        embed_fn=rollout_env.deterministic_fake_embed,
        dsn=graph["dsn"],
    )
    env.reset(task_kind="asof_test", task_input={"question": "what does settle depend on?"})
    return env


def test_dependency_closure_honours_the_episodes_pinned_commit(
    two_commit_graph: dict[str, Any],
) -> None:
    """The episode is pinned BEFORE the retirement, so replaying it must
    still see the path it was scored against."""
    env = _env(two_commit_graph)
    _retire_the_dependency(two_commit_graph)

    closure = env.kg_dependency_closure(node_key="act.settle")
    assert "sys.ledger" in {r["node_key"] for r in closure["closure"]}


def test_impact_radius_honours_the_episodes_pinned_commit(
    two_commit_graph: dict[str, Any],
) -> None:
    env = _env(two_commit_graph)
    _retire_the_dependency(two_commit_graph)

    impact = env.kg_impact_radius(node_key="sys.ledger")
    assert "act.settle" in {r["node_key"] for r in impact["impact"]}


def test_an_episode_pinned_after_the_retirement_does_not_see_the_path(
    two_commit_graph: dict[str, Any],
) -> None:
    """The other direction, which is what makes the two tests above mean
    something: pinning is doing the work, not a query that happens to
    return everything ever."""
    _retire_the_dependency(two_commit_graph)
    env = _env(two_commit_graph)

    closure = env.kg_dependency_closure(node_key="act.settle")
    assert "sys.ledger" not in {r["node_key"] for r in closure["closure"]}
    impact = env.kg_impact_radius(node_key="sys.ledger")
    assert "act.settle" not in {r["node_key"] for r in impact["impact"]}


def test_traverse_was_already_pinned_and_still_is(two_commit_graph: dict[str, Any]) -> None:
    """`kg.traverse` has taken `p_as_of` since db/008; this guards against a
    regression while the closure/radius call sites were being changed."""
    env = _env(two_commit_graph)
    _retire_the_dependency(two_commit_graph)

    result = env.kg_traverse(start_keys=["act.settle"], max_hops=2)
    assert "sys.ledger" in {r["node_key"] for r in result["nodes"]}
