"""tools/evidence.py -- registering evidence sources and ingesting their text.

These three tools are the write half of the retrieval story. `kg.hybrid_search`
(db/008_retrieval.sql) fuses four ranked lists, and one of them -- List C, the
evidence-chunk ANN arm -- reads `kg.chunk`. Before this module existed nothing
in the platform could put a row in that table: `db/010` gave `fde_agent` INSERT
on `kg.source` ("may register evidence it captured") but nothing on `kg.chunk`,
so an agent could register a source it could never attach text to, and the
third retrieval granularity was permanently empty. `db/014` adds the two
INSERT grants; this module is what uses them.

INSERT-only, on purpose
------------------------
There is no `kg_update_chunk` and there will not be one. Chunks are the
verbatim evidence a reviewer reads to check a proposal, so an agent that could
rewrite a chunk could rewrite the justification for a claim a human already
agreed to. Re-chunking a source means registering a NEW `kg.source` (which is
also what makes the re-chunk auditable); the old text stays where it was. The
grant in db/014 is INSERT only precisely so this is enforced by Postgres and
not by this module's good intentions -- a conflicting `(source_id, ordinal)`
is reported back as a skipped ordinal, never merged over.

Anchor keys and the invisibility warning
-----------------------------------------
`kg.hybrid_search`'s chunk arm keeps only rows with
`cardinality(anchor_keys) > 0` (db/008_retrieval.sql:315-319) -- a chunk with
no anchors contributes nothing to any node's rank, so it can be embedded,
indexed, and completely unreachable through `kg_search`. Empty `anchor_keys`
is still ALLOWED (a transcript is often chunked before extraction knows what
the chunks evidence), but `kg_ingest_chunks` always warns and names the exact
ordinals, because the alternative -- silence -- produces an ingestion that
looks successful and yields nothing.

The contract, not the tools, is the shared thing
-------------------------------------------------
The console's transcript intake writes the same two tables under a different
role (`fde_gate_service`, db/017). Everything a caller can observe about that
write -- the size caps, the SQL, the checksum-collision behaviour, the exact
warning text -- lives in `fde_mcp.ingest` so both writers cannot drift apart.
What stays here is the MCP surface: the pydantic schema a model fills in, the
docstrings it reads as prompts, and the trace emission.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from fde_mcp import db
from fde_mcp.ingest import (
    CHUNK_CONTENT_MAX_CHARS,
    DUPLICATE_SOURCE_NOTE,
    ENQUEUE_CHUNKS_SQL,
    INSERT_CHUNKS_SQL,
    INSERT_SOURCE_SQL,
    MAX_CHUNKS_PER_CALL,
    SOURCE_BY_CHECKSUM_SQL,
    SOURCE_KINDS,
    UNKNOWN_ANCHORS_SQL,
    skipped_ordinals_warning,
    unanchored_warning,
    unknown_anchors_warning,
)
from fde_mcp.tools._base import (
    emit_trace,
    fetchall,
    fetchone,
    now_ms,
    parse_timestamp,
    pg_error_boundary,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["CHUNK_CONTENT_MAX_CHARS", "MAX_CHUNKS_PER_CALL", "SOURCE_KINDS", "ChunkIn", "register"]

# Spelled as a Literal over the shared tuple so a bad kind is rejected by the
# tool schema with the valid list in the message, rather than reaching Postgres
# and coming back as a check-violation the model has to guess the vocabulary
# from. Same `Literal[TUPLE]` indirection as _base.NodeType -- see the comment
# there.
SourceKind = Literal[SOURCE_KINDS]  # type: ignore[valid-type]


class ChunkIn(BaseModel):
    """One chunk of evidence text inside a `kg_ingest_chunks` call."""

    ordinal: Annotated[int, Field(ge=0, description="0-based position within the source")]
    content: Annotated[str, Field(min_length=1, max_length=CHUNK_CONTENT_MAX_CHARS)]
    anchor_keys: list[str] = Field(
        default_factory=list,
        description=(
            "node_keys this chunk is evidence FOR. A chunk with no anchors is "
            "invisible to kg_search's chunk arm -- pass the keys the text "
            "actually supports."
        ),
    )
    token_count: int | None = None
    metadata: dict[str, Any] | None = None


@pg_error_boundary
async def kg_register_source(
    engagement_id: str,
    *,
    source_kind: SourceKind,
    title: str,
    captured_at: str,
    uri: str | None = None,
    captured_by: str | None = None,
    checksum: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register a piece of evidence (an interview, an SOP, a system export) so
    its text can be ingested and its source_id cited.

    Call this BEFORE kg_ingest_chunks (which needs the source_id) and before
    kg_propose (whose items each require >= 1 source_ids entry -- an
    unsourced item cannot be submitted). `captured_at` is an ISO-8601
    timestamp: when the evidence was captured from the business, NOT now.

    Pass `checksum` (sha256 of the raw artifact) whenever you have one. If a
    source with the same checksum already exists for this engagement, this
    returns that existing source_id with `created: false` instead of creating
    a duplicate -- so re-processing the same document twice is safe and its
    chunks stay attached to one source rather than being split across two.
    """
    t0 = now_ms()
    captured_ts = parse_timestamp(captured_at)
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            if checksum:
                await cur.execute(
                    SOURCE_BY_CHECKSUM_SQL,
                    {"eng": engagement_id, "checksum": checksum},
                )
                existing = await fetchone(cur)
                if existing is not None:
                    result: dict[str, Any] = {
                        "source_id": existing["source_id"],
                        "created": False,
                        "note": DUPLICATE_SOURCE_NOTE,
                        "existing": existing,
                    }
                    await emit_trace(
                        conn, "kg_register_source", result, latency_ms=int(now_ms() - t0)
                    )
                    return result

            await cur.execute(
                INSERT_SOURCE_SQL,
                {
                    "eng": engagement_id,
                    "kind": source_kind,
                    "uri": uri,
                    "title": title,
                    "captured_at": captured_ts,
                    "captured_by": captured_by,
                    "checksum": checksum,
                    "metadata": Jsonb(metadata or {}),
                },
            )
            row = await fetchone(cur)
            assert row is not None, "RETURNING always yields exactly one row here"

        result = {"source_id": row["source_id"], "created": True, "source": row}
        await emit_trace(conn, "kg_register_source", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def kg_ingest_chunks(
    engagement_id: str,
    source_id: int,
    chunks: Annotated[list[ChunkIn], Field(min_length=1, max_length=MAX_CHUNKS_PER_CALL)],
) -> dict[str, Any]:
    """Attach evidence text to a registered source and queue it for embedding.

    Each chunk needs an `ordinal` (its position in the source) and `content`
    (the verbatim text, <= 8000 chars -- split longer passages into more
    chunks rather than truncating). `anchor_keys` is the important field: the
    node_keys this passage is evidence for. It is what connects the text back
    to the graph, and kg_search's chunk arm DISCARDS chunks with no anchors,
    so a chunk ingested without them is stored but unreachable. You will get
    a warning naming those ordinals; ingest them anyway only if you genuinely
    do not yet know what they support, and re-register a new source once you
    do.

    Chunks are IMMUTABLE. Re-sending an ordinal that already exists for this
    source does nothing and is reported in `skipped_ordinals` -- it is not an
    error and it does not overwrite the stored text. To re-chunk a document,
    register a new source with kg_register_source.

    Returns the inserted chunk_ids, how many were queued for embedding, and a
    `warnings` list (empty anchors, anchor keys that match no live node --
    the latter is a warning, not a failure, since the node may still be
    sitting in an unmerged proposal).
    """
    t0 = now_ms()
    payload = [
        {
            "ordinal": c.ordinal,
            "content": c.content,
            "token_count": c.token_count,
            "anchor_keys": c.anchor_keys,
            "metadata": c.metadata or {},
        }
        for c in chunks
    ]
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            # The source must exist AND belong to this engagement. Without the
            # engagement check, a caller with a source_id from another
            # engagement would silently write chunks whose engagement_id
            # disagrees with their source's -- invisible until a search in one
            # engagement surfaced the other's evidence.
            await cur.execute(
                "SELECT source_id, title, source_kind FROM kg.source "
                "WHERE source_id = %(sid)s AND engagement_id = %(eng)s::uuid",
                {"sid": source_id, "eng": engagement_id},
            )
            source = await fetchone(cur)
            if source is None:
                result: dict[str, Any] = {
                    "error": "source not found for this engagement",
                    "hint": (
                        "register it with kg_register_source first, or check "
                        "engagement_id -- a source_id from a different "
                        "engagement is rejected here deliberately"
                    ),
                }
                await emit_trace(conn, "kg_ingest_chunks", result, latency_ms=int(now_ms() - t0))
                return result

            await cur.execute(
                INSERT_CHUNKS_SQL,
                {"eng": engagement_id, "sid": source_id, "chunks": Jsonb(payload)},
            )
            inserted = await fetchall(cur)
            chunk_ids = [r["chunk_id"] for r in inserted]
            inserted_ordinals = {r["ordinal"] for r in inserted}

            enqueued = 0
            if chunk_ids:
                await cur.execute(
                    ENQUEUE_CHUNKS_SQL,
                    {"eng": engagement_id, "ids": chunk_ids},
                )
                enqueued = len(await fetchall(cur))

            all_anchors = sorted({key for c in chunks for key in c.anchor_keys})
            unknown_anchors: list[str] = []
            if all_anchors:
                await cur.execute(
                    UNKNOWN_ANCHORS_SQL,
                    {"keys": all_anchors, "eng": engagement_id},
                )
                unknown_anchors = [r["node_key"] for r in await fetchall(cur)]

        skipped_ordinals = sorted(c.ordinal for c in chunks if c.ordinal not in inserted_ordinals)
        unanchored_ordinals = sorted(c.ordinal for c in chunks if not c.anchor_keys)

        warnings: list[str] = []
        if unanchored_ordinals:
            warnings.append(unanchored_warning(unanchored_ordinals))
        if unknown_anchors:
            warnings.append(unknown_anchors_warning(unknown_anchors))
        if skipped_ordinals:
            warnings.append(skipped_ordinals_warning(skipped_ordinals, source_id))

        result = {
            "source_id": source_id,
            "ingested": len(chunk_ids),
            "chunk_ids": chunk_ids,
            "skipped_ordinals": skipped_ordinals,
            "enqueued": enqueued,
            "warnings": warnings,
        }
        await emit_trace(conn, "kg_ingest_chunks", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def kg_list_sources(
    engagement_id: str,
    source_kind: SourceKind | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """List the evidence sources registered for this engagement, newest
    capture first, with how many chunks each has and how many of those are
    embedded yet.

    Use this to find the source_id to cite in a proposal's `source_ids`, to
    check whether a document has already been ingested before re-ingesting
    it, and to see whether a source's chunks have made it through the
    embedder (`embedded_chunks` < `chunk_count` means the queue has not
    drained yet, so kg_search will not surface that text at chunk
    granularity yet).
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.source_id, s.source_kind, s.title, s.uri, s.captured_at,
                       s.captured_by, s.checksum, s.metadata, s.created_at,
                       count(c.chunk_id)                                     AS chunk_count,
                       count(c.chunk_id) FILTER (WHERE c.embedding IS NOT NULL)
                                                                             AS embedded_chunks
                  FROM kg.source s
                  LEFT JOIN kg.chunk c ON c.source_id = s.source_id
                 WHERE s.engagement_id = %(eng)s::uuid
                   AND (%(kind)s::text IS NULL OR s.source_kind = %(kind)s::text)
                 GROUP BY s.source_id
                 ORDER BY s.captured_at DESC, s.source_id DESC
                 LIMIT %(limit)s
                """,
                {"eng": engagement_id, "kind": source_kind, "limit": limit},
            )
            rows = await fetchall(cur)
        result = {
            "engagement_id": engagement_id,
            "source_kind": source_kind,
            "returned": len(rows),
            "sources": rows,
        }
        await emit_trace(conn, "kg_list_sources", result, latency_ms=int(now_ms() - t0))
        return result


def register(mcp: FastMCP[None]) -> None:
    """Register every evidence tool on `mcp`."""
    mcp.tool()(kg_register_source)
    mcp.tool()(kg_ingest_chunks)
    mcp.tool()(kg_list_sources)
