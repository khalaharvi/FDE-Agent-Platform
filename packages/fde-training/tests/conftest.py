"""Shared fixtures for the fde_training test suite.

Design
------
Most of this package's tests are pure-function (the reward terms,
`export_sft`'s mask/dedup/split logic, `fde_training.config`, the QA reward
adapters) and need neither a live Postgres nor AWS credentials -- that is
the "no heavy imports, no live dependencies" contract documented in
`fde_training.rewards._episode`. The `requires_db` marker (registered in
the workspace root `pyproject.toml`, shared with `fde_mcp`/`fde_agents`)
covers the ones that do; `pytest_collection_modifyitems` skips those
cleanly when `FDE_DB_DSN` is unset, matching `fde_mcp`'s own conftest,
rather than letting each fail with a connection error.

`seeded_graph` mirrors `packages/fde-mcp/tests/conftest.py`'s `seed`
deliberately: the parity test compares this package's retrieval against
that package's, and comparing them over two differently-shaped fixture
graphs would leave "the graphs differ" as a live explanation for any
divergence it found. Same node set, same edge set, same deterministic
vector construction. Inserts run as the calling OS role (owner-equivalent
per `db/010_roles_and_seed_policy.sql`) on an autocommit connection,
standing in for what `hitl.merge_proposal` would eventually write.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

os.environ.setdefault("FDE_AGENT_NAME", "engagement")
os.environ.setdefault("FDE_AGENT_RUNTIME_ARN", "pytest:fde-training-tests")

from fde_mcp import embeddings
from fde_mcp.config import get_settings
from fde_training.rollout_env import RolloutEnv


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every `requires_db` test cleanly when no DSN is configured."""
    if os.environ.get("FDE_DB_DSN"):
        return
    skip_db = pytest.mark.skip(reason="FDE_DB_DSN not set; no live Postgres to test against")
    for item in items:
        if "requires_db" in item.keywords:
            item.add_marker(skip_db)


# `role.deal_desk` carries `is_role_title` because `kg.node`'s
# `node_role_is_not_a_person` check refuses a role node without it -- the
# schema-level enforcement of "translate a named individual into the role
# they occupy" that every agent prompt also states.
NODES: list[tuple[str, str, str, str, dict[str, Any]]] = [
    (
        "proc.quote_to_cash",
        "process",
        "Quote to Cash",
        "End-to-end quoting through booked revenue.",
        {},
    ),
    ("act.create_quote", "activity", "Create Quote", "Sales rep builds a quote in CPQ.", {}),
    (
        "act.discount_review",
        "activity",
        "Discount Review",
        "Deal desk reviews quotes discounted beyond policy.",
        {},
    ),
    (
        "ctl.discount_threshold_20",
        "control",
        "20% Discount Threshold",
        "Quotes above 20% discount require deal desk approval before send.",
        {},
    ),
    ("sys.cpq", "system", "CPQ", "Configure-price-quote system of record for quotes.", {}),
    (
        "role.deal_desk",
        "role",
        "Deal Desk Analyst",
        "Reviews and approves non-standard pricing.",
        {"is_role_title": True},
    ),
]

EDGES: list[tuple[str, str, str]] = [
    ("act.create_quote", "belongs_to", "proc.quote_to_cash"),
    ("act.discount_review", "belongs_to", "proc.quote_to_cash"),
    ("act.create_quote", "precedes", "act.discount_review"),
    ("act.create_quote", "gated_by", "ctl.discount_threshold_20"),
    ("act.discount_review", "depends_on", "sys.cpq"),
    ("role.deal_desk", "performs", "act.discount_review"),
]

# The vector the fake embed returns is identical to `act.discount_review`'s
# seeded embedding, so the node-ANN list is guaranteed to surface that node
# at rank 1 with a verifiable provenance entry -- the same trick
# `fde_mcp`'s conftest uses, for the same reason.
QUERY_VECTOR_SEED = 2
EMBEDDED_NODE_SEEDS = {
    "act.discount_review": QUERY_VECTOR_SEED,
    "act.create_quote": 5,
    "ctl.discount_threshold_20": 9,
}


def _fake_vector(seed: int, dims: int) -> list[float]:
    """Deterministic point on the unit sphere -- stable and distinguishable
    across seeds, which is all pgvector cosine search needs to be exercised
    for real without a live Bedrock call."""
    vec = [math.sin(seed * 12.9898 + i * 78.233) for i in range(dims)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture(scope="session")
def db_dsn() -> str:
    return os.environ["FDE_DB_DSN"]


@pytest.fixture(scope="session")
def seeded_graph(db_dsn: str) -> dict[str, Any]:
    """One sealed commit's worth of Quote-to-Cash graph, plus embeddings."""
    dims = get_settings().embedding.dimensions
    engagement_id = str(uuid.uuid4())
    node_type_by_key = {key: ntype for key, ntype, *_ in NODES}

    with (
        psycopg.connect(db_dsn, autocommit=True, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            """INSERT INTO kg.commit (engagement_id, status, title, authored_by, sealed_by, sealed_at)
               VALUES (%(eng)s, 'sealed', 'genesis (fde-training fixture)', 'pytest', 'pytest', now())
               RETURNING commit_id, sealed_at""",
            {"eng": engagement_id},
        )
        commit = cur.fetchone()
        assert commit is not None

        node_ids: dict[str, int] = {}
        for node_key, node_type, label, summary, attrs in NODES:
            cur.execute(
                """INSERT INTO kg.node
                       (engagement_id, node_key, node_type, label, summary, attributes, commit_id)
                   VALUES (%(eng)s, %(key)s, %(ntype)s, %(label)s, %(summary)s, %(attrs)s, %(cid)s)
                   RETURNING node_id""",
                {
                    "eng": engagement_id,
                    "key": node_key,
                    "ntype": node_type,
                    "label": label,
                    "summary": summary,
                    "attrs": Jsonb(attrs),
                    "cid": commit["commit_id"],
                },
            )
            row = cur.fetchone()
            assert row is not None
            node_ids[node_key] = row["node_id"]

        for src, edge_type, dst in EDGES:
            cur.execute(
                """INSERT INTO kg.edge
                       (engagement_id, edge_key, src_key, dst_key, edge_type, commit_id, human_confirmed)
                   VALUES (%(eng)s, %(ekey)s, %(src)s, %(dst)s, %(etype)s, %(cid)s, true)""",
                {
                    "eng": engagement_id,
                    "ekey": f"{src}|{edge_type}|{dst}",
                    "src": src,
                    "dst": dst,
                    "etype": edge_type,
                    "cid": commit["commit_id"],
                },
            )

        for node_key, vseed in EMBEDDED_NODE_SEEDS.items():
            cur.execute(
                """INSERT INTO kg.node_embedding
                       (node_id, engagement_id, node_type, embed_text, embedding,
                        model_id, model_dims, normalized, is_current)
                   VALUES (%(nid)s, %(eng)s, %(ntype)s, %(text)s, %(vec)s::kg.embedding,
                           'test-fixture', %(dims)s, true, true)""",
                {
                    "nid": node_ids[node_key],
                    "eng": engagement_id,
                    "ntype": node_type_by_key[node_key],
                    "text": f"{node_key} fixture text",
                    "vec": embeddings.to_pgvector_literal(_fake_vector(vseed, dims)),
                    "dims": dims,
                },
            )

    return {
        "engagement_id": engagement_id,
        "commit_id": commit["commit_id"],
        "sealed_at": commit["sealed_at"],
        "node_ids": node_ids,
        "query_vector": _fake_vector(QUERY_VECTOR_SEED, dims),
        "dsn": db_dsn,
        "role": "fde_rl_rollout",
    }


@pytest.fixture(autouse=True, scope="session")
def _close_rollout_pool() -> Iterator[None]:
    """`RolloutEnv.shared_pool()` is a class-level singleton by design (one
    pool for N concurrent rollouts). Closing it at session end keeps
    `filterwarnings = ["error"]` from turning a GC-time ResourceWarning into
    a failure in whichever test happens to run last."""
    yield
    if RolloutEnv._pool is not None:
        RolloutEnv._pool.close()
        RolloutEnv._pool = None
