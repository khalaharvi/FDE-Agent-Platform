"""Shared fixtures for the fde_mcp test suite.

Design
------
Every test that touches Postgres is marked `@pytest.mark.requires_db`
(applied file-wide via a `pytestmark` in each DB-backed test module).
`pytest_collection_modifyitems` below turns that marker into a clean
`SKIPPED` when `FDE_DB_DSN` is not set, so `pytest packages/fde-mcp/tests`
works out of the box in an environment with no database -- the alternative
(each test independently trying to connect and failing) would report 27
"errors" that are really just "no DB configured", drowning out real
failures.

Fixture data (`engagement_id`, `seed`) is session-scoped and inserted with
a plain synchronous `psycopg.connect(...)` as the calling OS role (which
`db/010_roles_and_seed_policy.sql` gives owner-equivalent rights),
deliberately bypassing `SET LOCAL ROLE fde_agent` -- this stands in for
what `hitl.merge_proposal` would eventually write, and lets every test
module below share one graph/commit/source without re-seeding.

`db`'s connection pool is a different story: it is async and is a
process-wide singleton bound to whichever event loop first opens it.
`asyncio_mode = "auto"` gives every async test its own function-scoped
event loop by default, so `_reset_db_pool` closes the pool after each test
-- the next test then opens a fresh pool on ITS OWN loop instead of
reusing one pinned to an already-closed loop. Pinning every test file to
one shared session-scoped loop was the alternative; closing per-test is
the one that stays correct no matter what order tests run in or which
files get selected with `-k`.

Because `kg_search` embeds its query via Bedrock and there is no live
Bedrock access in test environments, `fde_mcp.embeddings.embed`/
`embed_batch` are monkeypatched here to deterministic local vectors.
Everything downstream of that (pgvector HNSW search, RRF fusion, graph
expansion) is exercised for real.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

os.environ.setdefault("FDE_AGENT_NAME", "engagement")
os.environ.setdefault("FDE_AGENT_RUNTIME_ARN", "pytest:fde-mcp-tests")

from fde_mcp import db, embeddings
from fde_mcp.config import get_settings

DIMS = get_settings().embedding.dimensions


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every `requires_db` test cleanly when no DSN is configured."""
    if os.environ.get("FDE_DB_DSN"):
        return
    skip_db = pytest.mark.skip(reason="FDE_DB_DSN not set; no live Postgres to test against")
    for item in items:
        if "requires_db" in item.keywords:
            item.add_marker(skip_db)


@pytest_asyncio.fixture(autouse=True)
async def _reset_db_pool() -> AsyncIterator[None]:
    """See module docstring: close the async pool after every test so the
    next test (possibly on a different event loop) opens its own.
    """
    yield
    await db.close_pool()


def _fake_vector(seed: int, dims: int = DIMS) -> list[float]:
    """Deterministic point on the unit sphere. Not semantically meaningful --
    just stable and distinguishable across seeds, which is all pgvector
    cosine search needs to be exercised for real without a live Bedrock call.
    """
    vec = [math.sin(seed * 12.9898 + i * 78.233) for i in range(dims)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# The vector our monkeypatched embed() always returns -- deliberately
# IDENTICAL to the seeded embedding for "act.discount_approval", so
# kg_search's node-ANN list is guaranteed to surface that node at rank 1
# with a verifiable node_ann provenance entry.
QUERY_VECTOR_SEED = 2
_QUERY_VECTOR = _fake_vector(QUERY_VECTOR_SEED)


async def _fake_embed(
    text: str, *, input_type: str = "search_query", model_id: str | None = None
) -> list[float]:
    return _QUERY_VECTOR


async def _fake_embed_batch(
    texts: list[str], input_type: str = "search_document", model_id: str | None = None
) -> list[list[float]]:
    return [_QUERY_VECTOR for _ in texts]


@pytest.fixture(autouse=True, scope="session")
def _fake_bedrock() -> Iterator[None]:
    original_embed, original_embed_batch = embeddings.embed, embeddings.embed_batch
    embeddings.embed = _fake_embed
    embeddings.embed_batch = _fake_embed_batch
    try:
        yield
    finally:
        embeddings.embed = original_embed
        embeddings.embed_batch = original_embed_batch


NODES: list[tuple[str, str, str, str, dict[str, Any]]] = [
    (
        "proc.quote_to_cash",
        "process",
        "Quote to Cash",
        "End-to-end quoting and closing process.",
        {},
    ),
    (
        "act.legal_review",
        "activity",
        "Legal Review",
        "Legal reviews contract terms before signature.",
        {},
    ),
    (
        "act.discount_approval",
        "activity",
        "Discount Approval",
        "Deal desk approves discounts over 20 percent.",
        {},
    ),
    (
        "role.deal_desk",
        "role",
        "Deal Desk Analyst",
        "Approves non-standard discounts.",
        {"is_role_title": True},
    ),
    (
        "sys.salesforce_cpq",
        "system",
        "Salesforce CPQ",
        "Configure-price-quote system of record.",
        {},
    ),
    (
        "ctrl.discount_threshold",
        "control",
        "Discount Threshold Control",
        "Requires approval above 20 percent discount.",
        {},
    ),
]

EDGES: list[tuple[str, str, str]] = [
    ("act.legal_review", "precedes", "act.discount_approval"),
    ("role.deal_desk", "performs", "act.discount_approval"),
    ("act.discount_approval", "belongs_to", "proc.quote_to_cash"),
    ("act.legal_review", "belongs_to", "proc.quote_to_cash"),
    ("act.discount_approval", "gated_by", "ctrl.discount_threshold"),
    ("act.discount_approval", "depends_on", "sys.salesforce_cpq"),
]

# Nodes that get a real kg.node_embedding row (with a distinguishable seed).
EMBEDDED_NODE_SEEDS = {
    "act.discount_approval": QUERY_VECTOR_SEED,  # identical to the fake query vector
    "act.legal_review": 5,
    "ctrl.discount_threshold": 9,
}


@pytest.fixture(scope="session")
def engagement_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture(scope="session")
def seed(engagement_id: str) -> dict[str, Any]:
    dsn = os.environ["FDE_DB_DSN"]
    node_type_by_key = {key: ntype for key, ntype, *_ in NODES}
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kg.commit (engagement_id, status, title, authored_by, sealed_by, sealed_at)
            VALUES (%(eng)s, 'sealed', 'genesis (pytest fixture)', 'pytest', 'pytest', now())
            RETURNING commit_id, commit_uuid, content_digest, sealed_at
            """,
            {"eng": engagement_id},
        )
        commit = cur.fetchone()
        assert commit is not None
        commit_id = commit["commit_id"]

        cur.execute(
            """
            INSERT INTO kg.source (engagement_id, source_kind, title, captured_at)
            VALUES (%(eng)s, 'interview', 'pytest fixture interview', now())
            RETURNING source_id
            """,
            {"eng": engagement_id},
        )
        source_row = cur.fetchone()
        assert source_row is not None
        source_id = source_row["source_id"]

        node_ids: dict[str, int] = {}
        for node_key, node_type, label, summary, attrs in NODES:
            cur.execute(
                """
                INSERT INTO kg.node (engagement_id, node_key, node_type, label, summary, attributes, commit_id)
                VALUES (%(eng)s, %(key)s, %(ntype)s, %(label)s, %(summary)s, %(attrs)s, %(cid)s)
                RETURNING node_id
                """,
                {
                    "eng": engagement_id,
                    "key": node_key,
                    "ntype": node_type,
                    "label": label,
                    "summary": summary,
                    "attrs": Jsonb(attrs),
                    "cid": commit_id,
                },
            )
            node_row = cur.fetchone()
            assert node_row is not None
            node_ids[node_key] = node_row["node_id"]

        for src, etype, dst in EDGES:
            edge_key = f"{src}|{etype}|{dst}"
            cur.execute(
                """
                INSERT INTO kg.edge (engagement_id, edge_key, src_key, dst_key, edge_type, commit_id, human_confirmed)
                VALUES (%(eng)s, %(ekey)s, %(src)s, %(dst)s, %(etype)s, %(cid)s, true)
                """,
                {
                    "eng": engagement_id,
                    "ekey": edge_key,
                    "src": src,
                    "dst": dst,
                    "etype": etype,
                    "cid": commit_id,
                },
            )

        for node_key, vseed in EMBEDDED_NODE_SEEDS.items():
            vec = _fake_vector(vseed)
            cur.execute(
                """
                INSERT INTO kg.node_embedding
                    (node_id, engagement_id, node_type, embed_text, embedding,
                     model_id, model_dims, normalized, is_current)
                VALUES (%(nid)s, %(eng)s, %(ntype)s, %(text)s, %(vec)s::kg.embedding,
                        'test-fixture', %(dims)s, true, true)
                """,
                {
                    "nid": node_ids[node_key],
                    "eng": engagement_id,
                    "ntype": node_type_by_key[node_key],
                    "text": f"{node_key} fixture text",
                    "vec": embeddings.to_pgvector_literal(vec),
                    "dims": DIMS,
                },
            )

    return {
        "engagement_id": engagement_id,
        "commit_id": commit_id,
        "commit_uuid": str(commit["commit_uuid"]),
        "source_id": source_id,
        "node_ids": node_ids,
    }
