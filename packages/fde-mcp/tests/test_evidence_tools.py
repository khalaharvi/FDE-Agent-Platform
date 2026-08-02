"""Tests for fde_mcp.tools.evidence and the embedder worker's chunk branch.

These are the two halves of one path: `kg_register_source` +
`kg_ingest_chunks` write `kg.chunk` and queue it, `embedder_worker` drains
that queue, and only then does `kg_search`'s chunk arm have anything to
retrieve. The last test in this file walks the whole path end to end, which
is the thing that was impossible before -- `_embed_and_write` raised
`NotImplementedError` for `subject_kind='chunk'`, so a chunk could be
ingested and would never be embedded.

Bedrock is stubbed by conftest.py's session-scoped `_fake_bedrock`, which
returns the SAME vector for every input. That is what makes the round-trip
assertion deterministic: a chunk embedded by the worker ends up with a vector
identical to the one `kg_search` queries with, so it is guaranteed to be the
top hit of `kg.ann_chunks` and its anchor node must therefore carry a
`chunk_ann` provenance entry.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from fde_mcp import embedder_worker
from fde_mcp.tools import evidence, graph

pytestmark = pytest.mark.requires_db


def _dsn() -> str:
    """Read at call time, not import time -- `requires_db` skipping happens
    per test, and this module must still import with no DB configured.
    """
    return os.environ["FDE_DB_DSN"]


async def _drain_embed_queue() -> None:
    """Bounded rather than `while True` so a genuinely stuck queue fails the
    test instead of hanging the suite.
    """
    for _ in range(20):
        if await embedder_worker.run_once() == 0:
            return


async def _register(engagement_id: str, title: str, **kwargs: Any) -> dict[str, Any]:
    return await evidence.kg_register_source(
        engagement_id=engagement_id,
        source_kind="interview",
        title=title,
        captured_at="2026-01-15T09:30:00Z",
        **kwargs,
    )


# ===========================================================================
# kg_register_source
# ===========================================================================
async def test_register_source_inserts_and_returns_created(seed: dict[str, Any]) -> None:
    result = await _register(seed["engagement_id"], "Deal desk interview")
    assert "error" not in result
    assert result["created"] is True
    assert isinstance(result["source_id"], int)
    assert result["source"]["title"] == "Deal desk interview"


async def test_register_source_checksum_dedup_reuses_existing(seed: dict[str, Any]) -> None:
    checksum = f"sha256:{uuid.uuid4().hex}"
    first = await _register(seed["engagement_id"], "SOP v1", checksum=checksum)
    second = await _register(seed["engagement_id"], "SOP v1 (re-uploaded)", checksum=checksum)

    assert first["created"] is True
    assert second["created"] is False
    assert second["source_id"] == first["source_id"], (
        "the same checksum must resolve to one source, so its chunks stay together"
    )


# ===========================================================================
# kg_ingest_chunks
# ===========================================================================
async def test_ingest_chunks_inserts_and_enqueues(seed: dict[str, Any]) -> None:
    source = await _register(seed["engagement_id"], "Chunked transcript")
    result = await evidence.kg_ingest_chunks(
        engagement_id=seed["engagement_id"],
        source_id=source["source_id"],
        chunks=[
            evidence.ChunkIn(
                ordinal=0,
                content="Deal desk approves anything over twenty percent.",
                anchor_keys=["act.discount_approval"],
                token_count=9,
            ),
            evidence.ChunkIn(
                ordinal=1,
                content="Legal only sees the contract after the discount is signed off.",
                anchor_keys=["act.legal_review"],
            ),
        ],
    )
    assert "error" not in result
    assert result["ingested"] == 2
    assert result["enqueued"] == 2
    assert result["skipped_ordinals"] == []
    assert len(result["chunk_ids"]) == 2

    dsn = _dsn()
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT subject_kind, subject_id, completed_at FROM kg.embed_queue "
            "WHERE subject_kind = 'chunk' AND subject_id = ANY(%(ids)s)",
            {"ids": result["chunk_ids"]},
        )
        queued = cur.fetchall()
    assert len(queued) == 2
    assert all(q["completed_at"] is None for q in queued)


async def test_ingest_chunks_duplicate_ordinal_is_skipped_not_overwritten(
    seed: dict[str, Any],
) -> None:
    source = await _register(seed["engagement_id"], "Immutable source")
    original = "The original wording of this evidence."
    await evidence.kg_ingest_chunks(
        engagement_id=seed["engagement_id"],
        source_id=source["source_id"],
        chunks=[
            evidence.ChunkIn(ordinal=0, content=original, anchor_keys=["act.discount_approval"])
        ],
    )
    second = await evidence.kg_ingest_chunks(
        engagement_id=seed["engagement_id"],
        source_id=source["source_id"],
        chunks=[
            evidence.ChunkIn(
                ordinal=0,
                content="A rewritten version of the same evidence.",
                anchor_keys=["act.discount_approval"],
            )
        ],
    )
    assert "error" not in second
    assert second["ingested"] == 0
    assert second["skipped_ordinals"] == [0]
    assert any("immutable" in w for w in second["warnings"])

    with psycopg.connect(_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM kg.chunk WHERE source_id = %(sid)s ORDER BY ordinal",
            {"sid": source["source_id"]},
        )
        rows = cur.fetchall()
    assert [r["content"] for r in rows] == [original], "a re-sent ordinal must not rewrite evidence"


async def test_ingest_chunks_warns_about_empty_anchor_keys(seed: dict[str, Any]) -> None:
    source = await _register(seed["engagement_id"], "Unanchored transcript")
    result = await evidence.kg_ingest_chunks(
        engagement_id=seed["engagement_id"],
        source_id=source["source_id"],
        chunks=[
            evidence.ChunkIn(ordinal=0, content="Some text nobody has extracted from yet."),
            evidence.ChunkIn(
                ordinal=1, content="Anchored text.", anchor_keys=["act.discount_approval"]
            ),
        ],
    )
    assert result["ingested"] == 2
    invisibility = [w for w in result["warnings"] if "INVISIBLE" in w]
    assert len(invisibility) == 1, "an unanchored chunk must always be warned about"
    assert "[0]" in invisibility[0], "the warning must name the offending ordinals"
    assert "hybrid_search" in invisibility[0] or "kg_search" in invisibility[0]


async def test_ingest_chunks_fully_anchored_gets_no_invisibility_warning(
    seed: dict[str, Any],
) -> None:
    source = await _register(seed["engagement_id"], "Fully anchored transcript")
    result = await evidence.kg_ingest_chunks(
        engagement_id=seed["engagement_id"],
        source_id=source["source_id"],
        chunks=[
            evidence.ChunkIn(
                ordinal=0, content="All anchored.", anchor_keys=["act.discount_approval"]
            )
        ],
    )
    assert [w for w in result["warnings"] if "INVISIBLE" in w] == []


async def test_ingest_chunks_warns_about_unknown_anchor_keys(seed: dict[str, Any]) -> None:
    source = await _register(seed["engagement_id"], "Speculative anchors")
    result = await evidence.kg_ingest_chunks(
        engagement_id=seed["engagement_id"],
        source_id=source["source_id"],
        chunks=[
            evidence.ChunkIn(
                ordinal=0,
                content="Mentions a node that has not been merged yet.",
                anchor_keys=["act.discount_approval", "act.not_in_the_graph_yet"],
            )
        ],
    )
    assert result["ingested"] == 1, "an unknown anchor is a warning, never a rejection"
    unknown = [w for w in result["warnings"] if "match no live node" in w]
    assert len(unknown) == 1
    assert "act.not_in_the_graph_yet" in unknown[0]
    assert "act.discount_approval" not in unknown[0]


async def test_ingest_chunks_rejects_source_from_another_engagement(
    seed: dict[str, Any],
) -> None:
    source = await _register(seed["engagement_id"], "Belongs to the seeded engagement")
    result = await evidence.kg_ingest_chunks(
        engagement_id=str(uuid.uuid4()),
        source_id=source["source_id"],
        chunks=[evidence.ChunkIn(ordinal=0, content="Should never land.")],
    )
    assert "error" in result
    assert "hint" in result


# ===========================================================================
# kg_list_sources
# ===========================================================================
async def test_list_sources_reports_chunk_and_embedded_counts(seed: dict[str, Any]) -> None:
    source = await _register(seed["engagement_id"], "Counted source")
    await evidence.kg_ingest_chunks(
        engagement_id=seed["engagement_id"],
        source_id=source["source_id"],
        chunks=[
            evidence.ChunkIn(ordinal=0, content="One.", anchor_keys=["act.discount_approval"]),
            evidence.ChunkIn(ordinal=1, content="Two.", anchor_keys=["act.discount_approval"]),
        ],
    )
    result = await evidence.kg_list_sources(engagement_id=seed["engagement_id"])
    row = next(s for s in result["sources"] if s["source_id"] == source["source_id"])
    assert row["chunk_count"] == 2
    assert row["embedded_chunks"] == 0, "nothing is embedded until the worker runs"


async def test_list_sources_filters_by_kind(seed: dict[str, Any]) -> None:
    await evidence.kg_register_source(
        engagement_id=seed["engagement_id"],
        source_kind="sop_document",
        title="Discount policy SOP",
        captured_at="2026-01-10T00:00:00Z",
    )
    result = await evidence.kg_list_sources(
        engagement_id=seed["engagement_id"], source_kind="sop_document"
    )
    assert result["returned"] >= 1
    assert {s["source_kind"] for s in result["sources"]} == {"sop_document"}


# ===========================================================================
# Worker chunk branch -> kg_search round trip
# ===========================================================================
async def test_worker_embeds_chunk_and_it_surfaces_in_kg_search(seed: dict[str, Any]) -> None:
    """The full evidence path: register -> ingest -> embed -> retrieve.

    `sys.salesforce_cpq` deliberately has NO seeded node embedding
    (conftest.EMBEDDED_NODE_SEEDS), so if it comes back from `kg_search`
    carrying a `chunk_ann` provenance entry, that entry can only have come
    from the chunk this test ingested and the worker embedded.
    """
    source = await _register(seed["engagement_id"], "CPQ walkthrough recording")
    ingested = await evidence.kg_ingest_chunks(
        engagement_id=seed["engagement_id"],
        source_id=source["source_id"],
        chunks=[
            evidence.ChunkIn(
                ordinal=0,
                content="Every quote is built in Salesforce CPQ before it goes to deal desk.",
                anchor_keys=["sys.salesforce_cpq"],
            )
        ],
    )
    chunk_id = ingested["chunk_ids"][0]

    await _drain_embed_queue()

    with psycopg.connect(_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT embedding IS NOT NULL AS embedded, model_id FROM kg.chunk "
            "WHERE chunk_id = %(id)s",
            {"id": chunk_id},
        )
        chunk_row = cur.fetchone()
        cur.execute(
            "SELECT completed_at, last_error FROM kg.embed_queue "
            "WHERE subject_kind = 'chunk' AND subject_id = %(id)s",
            {"id": chunk_id},
        )
        queue_row = cur.fetchone()

    assert chunk_row is not None
    assert chunk_row["embedded"] is True, "the worker must write kg.chunk.embedding"
    assert chunk_row["model_id"], "the worker must record which model produced it"
    assert queue_row is not None
    assert queue_row["last_error"] is None
    assert queue_row["completed_at"] is not None

    result = await graph.kg_search(
        engagement_id=seed["engagement_id"], query="how are quotes built", k=20
    )
    hit = next(
        (r for r in result["results"] if r["node_key"] == "sys.salesforce_cpq"),
        None,
    )
    assert hit is not None, "the chunk's anchor node must be retrievable once embedded"
    assert "chunk_ann" in hit["provenance"], (
        "the node must be attributed to the chunk arm of kg.hybrid_search; "
        "without it the third retrieval granularity is still dead"
    )


async def test_worker_reports_a_vanished_chunk_as_a_failure(seed: dict[str, Any]) -> None:
    """A queue row pointing at a chunk that no longer exists must land in
    `last_error` via the normal `_mark_failed` path, not abort the batch.
    """
    # Negative and random: kg.chunk_id is always positive, and
    # kg.embed_queue is UNIQUE (subject_kind, subject_id), so a fixed literal
    # would collide with itself on a second run against the same database.
    vanished_id = -(uuid.uuid4().int % 2_000_000_000) - 1
    with (
        psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO kg.embed_queue (engagement_id, subject_kind, subject_id) "
            "VALUES (%(eng)s, 'chunk', %(id)s) RETURNING queue_id",
            {"eng": seed["engagement_id"], "id": vanished_id},
        )
        row = cur.fetchone()
    assert row is not None
    queue_id = row["queue_id"]

    await _drain_embed_queue()

    with psycopg.connect(_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT attempts, last_error, completed_at FROM kg.embed_queue WHERE queue_id = %(id)s",
            {"id": queue_id},
        )
        after = cur.fetchone()

    assert after is not None
    assert after["completed_at"] is None
    assert after["attempts"] >= 1
    assert "LookupError" in (after["last_error"] or "")
