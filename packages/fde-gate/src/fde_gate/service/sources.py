"""service/sources.py -- evidence intake from the console.

The console half of `kg_register_source` + `kg_ingest_chunks`. An operator
pastes an interview, this chunks it, anchors what it can, and writes the same
rows the MCP tools write -- same SQL, same checksum dedupe, same immutability
(`fde_mcp.ingest` holds all of it, so the two writers cannot drift).

Runs as `fde_gate_service`, which db/017 grants INSERT -- and only INSERT --
on `kg.source`, `kg.chunk` and `kg.embed_queue`. Nothing here can write
`kg.node`, `kg.edge` or `kg.commit`, and CI asserts that from the outside.
Registering evidence is a propose-side act: it puts text in front of the
agents and the reviewers, and it decides nothing.

Who may register evidence
--------------------------
Any ACTIVE reviewer, deliberately not just an administrator. The roster page
is admin-gated because editing it changes who may DECIDE; intake changes what
everyone can READ, which is the thing the platform wants more of, and an
interview that needs an administrator to upload it is an interview that sits
in someone's inbox. It is not anonymous either: `captured_by` records the
principal, so every chunk traces to a person, and deactivating a reviewer
ends their ability to add evidence along with everything else -- which is
what `set_active` already promises deactivation means.

Versioning, in a schema that has no version column
---------------------------------------------------
`kg.source` has no `version` or `supersedes` column and chunks are immutable,
so "re-ingest this source now that the anchors exist" cannot be an update. It
is a NEW source row, carrying its lineage in `metadata.intake`, holding
copies of the predecessor's still-dark chunks with the anchors the matcher
can now find.

That is not a workaround, it is the honest shape, and it has the property
that matters: **a passage is anchored in at most one source version.** Only
DARK chunks are ever carried forward, and a dark chunk is precisely one
`kg.hybrid_search` ignores (db/008_retrieval.sql:317), so the copy left
behind contributes nothing to any ranking. Re-ingesting cannot double-count,
because the only text eligible for it is text retrieval cannot see.

The old rows stay, dark, forever. That is the audit trail: what was ingested,
when, and what the graph could see at the time.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from http import HTTPStatus
from typing import Any

from psycopg.types.json import Jsonb

from fde_gate.config import get_gate_settings
from fde_gate.http import GateError
from fde_gate.intake import (
    Coverage,
    LiveNode,
    MatchedChunk,
    chunk_transcript,
    coverage_of,
    match_anchors,
)
from fde_gate.rows import fetchall, fetchone
from fde_mcp import db
from fde_mcp.ingest import (
    ENQUEUE_CHUNKS_SQL,
    INSERT_CHUNKS_SQL,
    INSERT_SOURCE_SQL,
    MAX_CHUNKS_PER_CALL,
    SOURCE_BY_CHECKSUM_SQL,
    SOURCE_KINDS,
)
from fde_mcp.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "DARK_CONSEQUENCE",
    "DARK_NEXT_STEP",
    "get_source",
    "list_sources",
    "preview",
    "register",
    "reingest_dark_chunks",
]

# The two sentences the whole feature exists to say. Split in two because the
# page shows them in that order -- what is true now, then what to do about it
# -- and because the second one is the only actionable half.
DARK_CONSEQUENCE = (
    "Retrieval will never see this text. kg_search keeps only chunks that "
    "anchor to at least one live node, so these passages are stored and "
    "embedded but cannot surface in any search, citation, or agent answer."
)
DARK_NEXT_STEP = (
    "Run the engagement agent's ingest_interview task against this source, "
    "review and merge the nodes it proposes, then come back here and use "
    "Re-ingest dark chunks."
)

_NOT_A_REVIEWER = (
    "{actor} is not an active reviewer, and registering evidence is "
    "attributed to a person. Ask an administrator to add you on /ui/reviewers "
    "(or to reactivate you), then try again."
)


def _gate_role() -> str:
    return get_gate_settings().gate.gate_role


async def _assert_active_reviewer(cur: Any, actor: str) -> None:
    """Refuse anyone who is not a live reviewer, in the caller's transaction.

    Inside the transaction that does the write, for the same reason
    `reviewers._assert_admin` is: a check on a separate connection leaves a
    window in which the reviewer is deactivated between the check and the
    INSERT.
    """
    if not actor:
        raise GateError(HTTPStatus.FORBIDDEN, _NOT_A_REVIEWER.format(actor="anonymous"))
    await cur.execute(
        "SELECT 1 AS ok FROM hitl.reviewer WHERE principal = %(p)s AND is_active",
        {"p": actor},
    )
    if await fetchone(cur) is None:
        raise GateError(HTTPStatus.FORBIDDEN, _NOT_A_REVIEWER.format(actor=actor))


def _checksum(parts: list[str]) -> str:
    """sha256 over content, with a separator no transcript can forge.

    The separator is a NUL byte: joining on it means two different chunk
    lists cannot collide by concatenating to the same string, and NUL cannot
    appear in a Postgres `text` value, so it cannot appear in the content
    being hashed either.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


async def _live_nodes(cur: Any, engagement_id: str) -> list[LiveNode]:
    """Every live node's key and label, for the anchor matcher.

    The same relation `kg.lexical_search` reads, filtered the same way, and
    read-only -- db/008 is not touched by this feature. The ranking is not
    reused; `intake.match_anchors` explains why.
    """
    await cur.execute(
        """
        SELECT n.node_key, n.label
          FROM kg.node_current n
         WHERE n.engagement_id = %(eng)s::uuid
         ORDER BY n.node_key
        """,
        {"eng": engagement_id},
    )
    return [LiveNode(node_key=r["node_key"], label=r["label"]) for r in await fetchall(cur)]


async def _known_engagements(cur: Any) -> list[str]:
    """Engagements worth suggesting in the picker. Nobody types a uuid."""
    await cur.execute(
        """
        SELECT DISTINCT engagement_id FROM (
          SELECT engagement_id FROM kg.commit
          UNION SELECT engagement_id FROM kg.source
          UNION SELECT engagement_id FROM hitl.proposal
        ) e ORDER BY engagement_id
        """
    )
    return [str(r["engagement_id"]) for r in await fetchall(cur)]


def _validate(*, source_kind: str, captured_at: str) -> datetime:
    """Check the two fields a browser cannot check for us.

    A bare date is returned NAIVE, deliberately. `<input type="date">` sends
    "2026-08-03" with no zone, and stamping UTC on it here would store
    midnight UTC -- which every deployment west of Greenwich then reads back
    as the 2nd. An operator who typed the 3rd and is shown the 2nd has no way
    to tell a timezone artefact from a bug in their own memory of the
    interview.

    Left naive, Postgres resolves it in the session's TimeZone, and the same
    setting resolves it on the way back out, so "the day you typed" and "the
    day it shows" agree by construction on any deployment. A caller who
    supplies an explicit offset means it and keeps it.
    """
    if source_kind not in SOURCE_KINDS:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            f"{source_kind!r} is not a source kind. Valid kinds: {', '.join(SOURCE_KINDS)}.",
        )
    try:
        # fromisoformat accepts both the date-only form and a full timestamp,
        # so an operator typing either is understood.
        return datetime.fromisoformat(captured_at)
    except ValueError:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            f"{captured_at!r} is not a date. Use YYYY-MM-DD -- the day the "
            "interview happened or the document was collected, not today.",
        ) from None


async def preview(
    actor: str,
    *,
    engagement_id: str,
    title: str,
    source_kind: str,
    captured_at: str,
    text: str,
) -> dict[str, Any]:
    """Chunk and anchor a transcript WITHOUT writing anything.

    The screen between paste and commit. It runs the identical chunker and
    matcher the write will run, so the numbers on it are the numbers that
    will be stored -- the chunker is deterministic and takes no input beyond
    the text, which is what makes a stateless preview honest rather than an
    estimate.
    """
    _validate(source_kind=source_kind, captured_at=captured_at)
    contents = chunk_transcript(text)
    if not contents:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            "there is no text to ingest -- the paste box was empty, or the "
            "uploaded file contained only whitespace.",
        )

    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_active_reviewer(cur, actor)
        nodes = await _live_nodes(cur, engagement_id)
        checksum = _checksum(contents)
        await cur.execute(SOURCE_BY_CHECKSUM_SQL, {"eng": engagement_id, "checksum": checksum})
        duplicate = await fetchone(cur)

    chunks = match_anchors(contents, nodes)
    return {
        "engagement_id": engagement_id,
        "title": title,
        "source_kind": source_kind,
        "captured_at": captured_at,
        "chunks": chunks,
        "coverage": coverage_of(chunks),
        "live_node_count": len(nodes),
        "checksum": checksum,
        "duplicate_of": duplicate,
    }


async def register(
    actor: str,
    *,
    engagement_id: str,
    title: str,
    source_kind: str,
    captured_at: str,
    text: str,
    filename: str | None = None,
) -> dict[str, Any]:
    """Register the source and write its chunks. One transaction.

    A source with chunks that failed to write would be exactly the state
    `kg_register_source`'s note warns about -- a source_id nothing is
    attached to -- so the two are one transaction here even though the MCP
    surface exposes them as two calls (an agent genuinely needs to register
    first and ingest later; an operator pasting a transcript does not).
    """
    captured = _validate(source_kind=source_kind, captured_at=captured_at)
    contents = chunk_transcript(text)
    if not contents:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            "there is no text to ingest -- the paste box was empty, or the "
            "uploaded file contained only whitespace.",
        )
    checksum = _checksum(contents)

    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_active_reviewer(cur, actor)

        await cur.execute(SOURCE_BY_CHECKSUM_SQL, {"eng": engagement_id, "checksum": checksum})
        existing = await fetchone(cur)
        if existing is not None:
            log.info("intake_duplicate", actor=actor, source_id=existing["source_id"])
            return {
                "created": False,
                "source_id": int(existing["source_id"]),
                "existing": existing,
                "coverage": None,
            }

        nodes = await _live_nodes(cur, engagement_id)
        chunks = match_anchors(contents, nodes)
        metadata: dict[str, Any] = {
            "intake": {
                "version": 1,
                "via": "console",
                "captured_from": "upload" if filename else "paste",
            }
        }
        if filename:
            metadata["intake"]["filename"] = filename

        source_id = await _insert_source(
            cur,
            engagement_id=engagement_id,
            source_kind=source_kind,
            title=title,
            captured_at=captured,
            captured_by=actor,
            checksum=checksum,
            metadata=metadata,
        )
        enqueued = await _insert_chunks(
            cur, engagement_id=engagement_id, source_id=source_id, chunks=chunks
        )

    coverage = coverage_of(chunks)
    log.info(
        "intake_registered",
        actor=actor,
        source_id=source_id,
        chunks=len(chunks),
        anchored=coverage.anchored,
    )
    return {
        "created": True,
        "source_id": source_id,
        "chunks": len(chunks),
        "enqueued": enqueued,
        "coverage": coverage,
    }


async def _insert_source(
    cur: Any,
    *,
    engagement_id: str,
    source_kind: str,
    title: str,
    captured_at: datetime,
    captured_by: str,
    checksum: str,
    metadata: dict[str, Any],
) -> int:
    await cur.execute(
        INSERT_SOURCE_SQL,
        {
            "eng": engagement_id,
            "kind": source_kind,
            # NULL: `kg.source.uri` is an s3:// pointer to a raw artifact, and
            # pasted text has no artifact to point at. The filename of an
            # upload goes in metadata, where it is a fact about provenance
            # rather than a location that does not resolve.
            "uri": None,
            "title": title,
            "captured_at": captured_at,
            "captured_by": captured_by,
            "checksum": checksum,
            "metadata": Jsonb(metadata),
        },
    )
    row = await fetchone(cur)
    assert row is not None, "RETURNING always yields exactly one row here"
    return int(row["source_id"])


async def _insert_chunks(
    cur: Any, *, engagement_id: str, source_id: int, chunks: list[MatchedChunk]
) -> int:
    """Write chunks in batches and queue them for embedding.

    Batched at `MAX_CHUNKS_PER_CALL` because that is the bound on one
    `jsonb_to_recordset` statement, not on a document -- a 300-chunk
    transcript is two statements, not a refusal.
    """
    enqueued = 0
    for start in range(0, len(chunks), MAX_CHUNKS_PER_CALL):
        batch = chunks[start : start + MAX_CHUNKS_PER_CALL]
        payload: list[dict[str, Any]] = [
            {
                "ordinal": c.ordinal,
                "content": c.content,
                "token_count": None,
                "anchor_keys": list(c.anchor_keys),
                "metadata": {},
            }
            for c in batch
        ]
        await cur.execute(
            INSERT_CHUNKS_SQL,
            {"eng": engagement_id, "sid": source_id, "chunks": Jsonb(payload)},
        )
        chunk_ids = [r["chunk_id"] for r in await fetchall(cur)]
        if chunk_ids:
            await cur.execute(ENQUEUE_CHUNKS_SQL, {"eng": engagement_id, "ids": chunk_ids})
            enqueued += len(await fetchall(cur))
    return enqueued


async def list_sources(actor: str, *, engagement_id: str | None = None) -> dict[str, Any]:
    """Every registered source with how much of it retrieval can see.

    `anchored_chunks` is computed with the same predicate
    `kg.hybrid_search`'s chunk arm uses -- `cardinality(anchor_keys) > 0` --
    so the percentage on this page is a statement about retrieval, not a
    proxy for one.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_active_reviewer(cur, actor)
        await cur.execute(
            """
            SELECT s.source_id, s.engagement_id, s.source_kind, s.title,
                   s.captured_at, s.captured_by, s.created_at,
                   (s.metadata #>> '{intake,reingest_of}')::bigint AS reingest_of,
                   COALESCE((s.metadata #>> '{intake,version}')::int, 1)  AS version,
                   count(c.chunk_id)                                      AS chunk_count,
                   count(c.chunk_id) FILTER (WHERE cardinality(c.anchor_keys) > 0)
                                                                          AS anchored_chunks,
                   count(c.chunk_id) FILTER (WHERE c.embedding IS NOT NULL)
                                                                          AS embedded_chunks
              FROM kg.source s
              LEFT JOIN kg.chunk c ON c.source_id = s.source_id
             WHERE (%(eng)s::uuid IS NULL OR s.engagement_id = %(eng)s::uuid)
             GROUP BY s.source_id
             ORDER BY s.captured_at DESC, s.source_id DESC
             LIMIT 200
            """,
            {"eng": engagement_id},
        )
        rows = await fetchall(cur)
        engagements = await _known_engagements(cur)

    sources = [_decorate(row) for row in rows]
    superseded_by = {
        row["reingest_of"]: row["source_id"] for row in rows if row["reingest_of"] is not None
    }
    for source in sources:
        source["superseded_by"] = superseded_by.get(source["source_id"])

    return {
        "engagement_id": engagement_id,
        "engagements": engagements,
        "sources": sources,
        "source_kinds": list(SOURCE_KINDS),
    }


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    """Attach the coverage view a template renders, to one listing row."""
    source = dict(row)
    source["source_id"] = int(row["source_id"])
    source["coverage"] = Coverage(
        total=int(row["chunk_count"]), anchored=int(row["anchored_chunks"])
    )
    return source


async def get_source(actor: str, source_id: int) -> dict[str, Any]:
    """One source, its chunks, its coverage, and where it sits in its lineage."""
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_active_reviewer(cur, actor)
        await cur.execute(
            """
            SELECT s.source_id, s.engagement_id, s.source_kind, s.title, s.uri,
                   s.captured_at, s.captured_by, s.checksum, s.metadata, s.created_at,
                   (s.metadata #>> '{intake,reingest_of}')::bigint AS reingest_of,
                   COALESCE((s.metadata #>> '{intake,version}')::int, 1) AS version
              FROM kg.source s
             WHERE s.source_id = %(sid)s
            """,
            {"sid": source_id},
        )
        source = await fetchone(cur)
        if source is None:
            return {"error": f"no source {source_id}"}

        await cur.execute(
            """
            SELECT c.chunk_id, c.ordinal, c.content, c.anchor_keys,
                   (c.embedding IS NOT NULL) AS embedded
              FROM kg.chunk c
             WHERE c.source_id = %(sid)s
             ORDER BY c.ordinal
            """,
            {"sid": source_id},
        )
        chunk_rows = await fetchall(cur)

        # The successor, if this version has already been re-ingested. Read
        # rather than assumed, so a chain built by two operators at once
        # still shows the truth on both pages.
        await cur.execute(
            """
            SELECT source_id FROM kg.source
             WHERE (metadata #>> '{intake,reingest_of}')::bigint = %(sid)s
             ORDER BY source_id
             LIMIT 1
            """,
            {"sid": source_id},
        )
        successor = await fetchone(cur)

    chunks = [
        MatchedChunk(
            ordinal=int(r["ordinal"]),
            content=str(r["content"]),
            anchor_keys=tuple(r["anchor_keys"] or ()),
        )
        for r in chunk_rows
    ]
    coverage = coverage_of(chunks)
    return {
        "source": dict(source),
        "source_id": int(source["source_id"]),
        "chunks": chunks,
        "chunk_rows": chunk_rows,
        "coverage": coverage,
        "superseded_by": None if successor is None else int(successor["source_id"]),
        "can_reingest": successor is None and coverage.dark > 0,
    }


async def reingest_dark_chunks(actor: str, source_id: int) -> dict[str, Any]:
    """Carry this source's still-dark chunks into a new version, re-anchored.

    Refuses rather than writing a second dark copy when the matcher still
    finds nothing: a new source that is also 0% covered is not progress, it
    is another row to explain. The refusal names what to do instead, which is
    the same next step the 0%-coverage banner names.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_active_reviewer(cur, actor)

        await cur.execute(
            """
            SELECT s.source_id, s.engagement_id, s.source_kind, s.title, s.captured_at,
                   COALESCE((s.metadata #>> '{intake,version}')::int, 1) AS version
              FROM kg.source s
             WHERE s.source_id = %(sid)s
            """,
            {"sid": source_id},
        )
        source = await fetchone(cur)
        if source is None:
            raise GateError(HTTPStatus.NOT_FOUND, f"no source {source_id}")

        await cur.execute(
            "SELECT source_id FROM kg.source "
            "WHERE (metadata #>> '{intake,reingest_of}')::bigint = %(sid)s LIMIT 1",
            {"sid": source_id},
        )
        successor = await fetchone(cur)
        if successor is not None:
            raise GateError(
                HTTPStatus.CONFLICT,
                f"source {source_id} has already been re-ingested as source "
                f"{successor['source_id']}. Re-ingest that one instead -- it "
                "holds whatever is still dark.",
            )

        await cur.execute(
            """
            SELECT ordinal, content FROM kg.chunk
             WHERE source_id = %(sid)s AND cardinality(anchor_keys) = 0
             ORDER BY ordinal
            """,
            {"sid": source_id},
        )
        dark_rows = await fetchall(cur)
        if not dark_rows:
            raise GateError(
                HTTPStatus.CONFLICT,
                f"every chunk of source {source_id} already anchors to a live "
                "node, so there is nothing dark to re-ingest.",
            )

        engagement_id = str(source["engagement_id"])
        nodes = await _live_nodes(cur, engagement_id)
        contents = [str(r["content"]) for r in dark_rows]
        chunks = match_anchors(contents, nodes)
        coverage = coverage_of(chunks)
        if coverage.anchored == 0:
            # Phrased without the counts on purpose: "none of the 1 dark
            # chunks match any of the 1 live nodes" is what putting them in
            # produces, and the page the operator is reading already shows
            # both numbers.
            raise GateError(
                HTTPStatus.CONFLICT,
                f"none of the dark passages in source {source_id} match any "
                "live node on this engagement, so re-ingesting now would "
                "create a second copy that retrieval also cannot see. "
                f"{DARK_NEXT_STEP}",
            )

        # Over content AND the anchors resolved for it: clicking twice with
        # nothing merged in between is the same document with the same
        # anchors, and must be a no-op. A newly merged node changes the
        # anchors, changes the checksum, and is a genuinely new version.
        checksum = _checksum(contents + [",".join(c.anchor_keys) for c in chunks])
        await cur.execute(SOURCE_BY_CHECKSUM_SQL, {"eng": engagement_id, "checksum": checksum})
        existing = await fetchone(cur)
        if existing is not None:
            return {
                "created": False,
                "source_id": int(existing["source_id"]),
                "predecessor_id": source_id,
                "coverage": None,
            }

        version = int(source["version"]) + 1
        new_id = await _insert_source(
            cur,
            engagement_id=engagement_id,
            source_kind=str(source["source_kind"]),
            title=f"{source['title']} (re-ingest v{version})",
            # The predecessor's capture time, not now: the interview happened
            # when it happened, and `captured_at` orders the evidence by when
            # the business said it, not by when we managed to anchor it.
            captured_at=source["captured_at"],
            captured_by=actor,
            checksum=checksum,
            metadata={
                "intake": {
                    "version": version,
                    "via": "console",
                    "captured_from": "reingest",
                    "reingest_of": source_id,
                }
            },
        )
        enqueued = await _insert_chunks(
            cur, engagement_id=engagement_id, source_id=new_id, chunks=chunks
        )

    log.info(
        "intake_reingested",
        actor=actor,
        predecessor_id=source_id,
        source_id=new_id,
        chunks=len(chunks),
        anchored=coverage.anchored,
    )
    return {
        "created": True,
        "source_id": new_id,
        "predecessor_id": source_id,
        "chunks": len(chunks),
        "enqueued": enqueued,
        "coverage": coverage,
    }
