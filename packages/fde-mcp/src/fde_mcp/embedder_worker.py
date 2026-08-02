"""embedder_worker.py -- async backfill worker draining kg.embed_queue.

Why this is a separate process from the merge path
----------------------------------------------------
hitl.merge_proposal (db/005_merge.sql) enqueues a row into kg.embed_queue for
every node/edge it writes, but does not embed anything itself -- a Bedrock
timeout or throttle must never block a human's approval from landing. This
worker is the other half of that contract: it claims queued rows with
`FOR UPDATE SKIP LOCKED` (safe to run N copies of this worker concurrently),
builds the text that gets embedded, calls Bedrock, and writes the result
into kg.node_embedding / kg.edge_embedding.

Verbalisation
--------------
* Nodes: "{label}. {summary}" (falls back to just the label if summary is
  null/empty) -- this is literally what gets embedded and is stored back
  into node_embedding.embed_text so a model change is a pure recompute with
  no join back to source (003_vectors_hnsw.sql's comment on that column).
* Edges: NOT the raw edge_type -- a natural-language verbalisation via
  `EDGE_VERBALISER`, e.g. "Sales Rep hands off Quote Approval to Deal Desk"
  reads far better to an embedding model than "hands_off_to" (see
  003_vectors_hnsw.sql's own comment on this). `_verbalise_edge` looks up
  both endpoint labels and renders `"{src label} {phrase} {dst label}"`,
  optionally folding in a branch condition from edge.attributes when present.
* Chunks: `kg.chunk.content` VERBATIM. There is nothing to verbalise -- a
  chunk already is the evidence text a human would read, and paraphrasing it
  would embed something other than what a reviewer sees. The 8000-character
  cap that `kg_ingest_chunks` enforces (tools/evidence.py) keeps every chunk
  inside the embedding model's input budget, which is why there is no
  truncation logic here; a chunk that arrived through some other path and is
  too long must fail loudly at Bedrock rather than be silently half-embedded.

Runs as a standalone process: `python -m fde_mcp.embedder_worker` (or the
`fde-embedder` console script). Polls kg.embed_queue on
`poll_interval_seconds`; drains until empty, then sleeps. Connects and
operates as `fde_ingest` (the role granted write access to
kg.node_embedding/kg.edge_embedding/kg.chunk/kg.embed_queue in
db/010_roles_and_seed_policy.sql) via the same SET LOCAL ROLE pattern as the
MCP server, reusing db.py's pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from fde_mcp import db, embeddings
from fde_mcp.config import get_settings
from fde_mcp.logging import configure_logging, get_logger

log = get_logger(__name__)

EMBED_INPUT_TYPE = "search_document"  # verbalised graph text is the indexed side

# ---------------------------------------------------------------------------
# Edge verbalisation. Every value in kg.edge_type (001_extensions_and_types.sql)
# has an entry -- missing one is a silent quality regression (the raw
# enum name would leak into embed_text), so `_phrase_for` fails loudly on an
# unmapped type instead of guessing.
# ---------------------------------------------------------------------------
EDGE_VERBALISER: dict[str, str] = {
    "belongs_to": "belongs to",
    "performs": "performs",
    "precedes": "happens before",
    "hands_off_to": "hands off to",
    "produces": "produces",
    "consumes": "consumes",
    "recorded_in": "is recorded in",
    "depends_on": "depends on",
    "gated_by": "is gated by",
    "measured_by": "is measured by",
    "blocks": "blocks",
    "addresses": "addresses",
    "automatable_by": "may be automated by",
    "evidenced_by": "is evidenced by",
    "supersedes": "supersedes",
}


def _phrase_for(edge_type: str) -> str:
    try:
        return EDGE_VERBALISER[edge_type]
    except KeyError as exc:
        msg = (
            f"no verbalisation phrase registered for kg.edge_type={edge_type!r}; "
            "add one to EDGE_VERBALISER in embedder_worker.py"
        )
        raise ValueError(msg) from exc


def _verbalise_node(label: str, summary: str | None) -> str:
    label = (label or "").strip()
    summary = (summary or "").strip()
    if summary:
        return f"{label}. {summary}"
    return label


def _verbalise_edge(
    src_label: str, edge_type: str, dst_label: str, attributes: dict[str, Any] | None
) -> str:
    phrase = _phrase_for(edge_type)
    text = f"{src_label} {phrase} {dst_label}"
    if attributes:
        condition = attributes.get("condition") or attributes.get("branch_condition")
        if condition:
            text += f", when {condition}"
        sla_seconds = attributes.get("sla_seconds")
        if sla_seconds:
            text += f" (SLA {sla_seconds}s)"
    return text


# ---------------------------------------------------------------------------
# Claiming and processing
# ---------------------------------------------------------------------------
async def _claim_batch(conn: db.Connection, limit: int) -> list[dict[str, Any]]:
    max_attempts = get_settings().embedder.max_attempts
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT queue_id, engagement_id, subject_kind, subject_id, attempts
              FROM kg.embed_queue
             WHERE completed_at IS NULL
               AND attempts < %(max_attempts)s
             ORDER BY enqueued_at
             LIMIT %(limit)s
             FOR UPDATE SKIP LOCKED
            """,
            {"limit": limit, "max_attempts": max_attempts},
        )
        rows = list(await cur.fetchall())
        if rows:
            await cur.execute(
                "UPDATE kg.embed_queue SET claimed_at = now() WHERE queue_id = ANY(%(ids)s)",
                {"ids": [r["queue_id"] for r in rows]},
            )
    return rows


async def _load_node_text(conn: db.Connection, node_id: int) -> tuple[dict[str, Any], str] | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT node_id, engagement_id, node_key, node_type, label, summary, "
            "valid_to, tx_to FROM kg.node WHERE node_id = %(id)s",
            {"id": node_id},
        )
        node = await cur.fetchone()
    if node is None:
        return None
    return node, _verbalise_node(node["label"], node["summary"])


async def _load_edge_text(conn: db.Connection, edge_id: int) -> tuple[dict[str, Any], str] | None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT e.edge_id, e.engagement_id, e.edge_type, e.attributes,
                   e.valid_to, e.tx_to,
                   src.label AS src_label, dst.label AS dst_label
              FROM kg.edge e
              JOIN kg.node_current src ON src.engagement_id = e.engagement_id AND src.node_key = e.src_key
              JOIN kg.node_current dst ON dst.engagement_id = e.engagement_id AND dst.node_key = e.dst_key
             WHERE e.edge_id = %(id)s
            """,
            {"id": edge_id},
        )
        edge = await cur.fetchone()
    if edge is None:
        return None
    text = _verbalise_edge(
        edge["src_label"], edge["edge_type"], edge["dst_label"], edge["attributes"]
    )
    return edge, text


async def _load_chunk(conn: db.Connection, chunk_id: int) -> dict[str, Any] | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT chunk_id, engagement_id, content FROM kg.chunk WHERE chunk_id = %(id)s",
            {"id": chunk_id},
        )
        return await cur.fetchone()


async def _mark_complete(conn: db.Connection, queue_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE kg.embed_queue SET completed_at = now() WHERE queue_id = %(id)s",
            {"id": queue_id},
        )


async def _mark_failed(conn: db.Connection, queue_id: int, error: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE kg.embed_queue
               SET attempts = attempts + 1, last_error = %(err)s, claimed_at = NULL
             WHERE queue_id = %(id)s
            """,
            {"id": queue_id, "err": error[:4000]},
        )


async def _upsert_node_embedding(
    conn: db.Connection, node: dict[str, Any], embed_text: str, vector: list[float], model_id: str
) -> None:
    is_current = node["valid_to"] is None and node["tx_to"] is None
    dims = get_settings().embedding.dimensions

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO kg.node_embedding
                (node_id, engagement_id, node_type, embed_text, embedding,
                 model_id, model_dims, normalized, is_current)
            VALUES (%(node_id)s, %(eng)s, %(node_type)s::kg.node_type, %(text)s,
                    %(vec)s::kg.embedding, %(model_id)s, %(dims)s, true, %(is_current)s)
            ON CONFLICT (node_id) DO UPDATE
              SET embed_text = EXCLUDED.embed_text,
                  embedding = EXCLUDED.embedding,
                  model_id = EXCLUDED.model_id,
                  model_dims = EXCLUDED.model_dims,
                  is_current = EXCLUDED.is_current,
                  embedded_at = now()
            """,
            {
                "node_id": node["node_id"],
                "eng": node["engagement_id"],
                "node_type": node["node_type"],
                "text": embed_text,
                "vec": embeddings.to_pgvector_literal(vector),
                "model_id": model_id,
                "dims": dims,
                "is_current": is_current,
            },
        )


async def _upsert_edge_embedding(
    conn: db.Connection, edge: dict[str, Any], embed_text: str, vector: list[float], model_id: str
) -> None:
    is_current = edge["valid_to"] is None and edge["tx_to"] is None

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO kg.edge_embedding
                (edge_id, engagement_id, edge_type, embed_text, embedding, model_id, is_current)
            VALUES (%(edge_id)s, %(eng)s, %(edge_type)s::kg.edge_type, %(text)s,
                    %(vec)s::kg.embedding, %(model_id)s, %(is_current)s)
            ON CONFLICT (edge_id) DO UPDATE
              SET embed_text = EXCLUDED.embed_text,
                  embedding = EXCLUDED.embedding,
                  model_id = EXCLUDED.model_id,
                  is_current = EXCLUDED.is_current,
                  embedded_at = now()
            """,
            {
                "edge_id": edge["edge_id"],
                "eng": edge["engagement_id"],
                "edge_type": edge["edge_type"],
                "text": embed_text,
                "vec": embeddings.to_pgvector_literal(vector),
                "model_id": model_id,
                "is_current": is_current,
            },
        )


async def _write_chunk_embedding(
    conn: db.Connection, chunk: dict[str, Any], vector: list[float], model_id: str
) -> None:
    """UPDATE, not INSERT ... ON CONFLICT like the node/edge paths.

    kg.chunk stores its embedding as a column on the chunk row itself
    (003_vectors_hnsw.sql) rather than in a side table, so there is no row to
    upsert and no `is_current`/`embedded_at` to maintain -- a chunk's text is
    immutable (see tools/evidence.py), so its embedding is either absent or
    correct for the current model, and re-embedding under a new model is a
    plain overwrite.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE kg.chunk
               SET embedding = %(vec)s::kg.embedding, model_id = %(model_id)s
             WHERE chunk_id = %(id)s
            """,
            {
                "id": chunk["chunk_id"],
                "vec": embeddings.to_pgvector_literal(vector),
                "model_id": model_id,
            },
        )


async def _embed_and_write(conn: db.Connection, item: dict[str, Any]) -> None:
    """The part of processing one queue row that can fail (missing row,
    Bedrock error, bad response shape). Raises on any problem; never calls
    `_mark_failed`/`_mark_complete` itself -- `_process_one` decides that
    once this returns cleanly or raises.
    """
    model_id = get_settings().embedding.model_id
    if item["subject_kind"] == "node":
        loaded = await _load_node_text(conn, item["subject_id"])
        if loaded is None:
            msg = f"node_id {item['subject_id']} no longer exists"
            raise LookupError(msg)
        node, text = loaded
        vector = await embeddings.embed(text, input_type=EMBED_INPUT_TYPE)
        await _upsert_node_embedding(conn, node, text, vector, model_id)
    elif item["subject_kind"] == "edge":
        loaded = await _load_edge_text(conn, item["subject_id"])
        if loaded is None:
            msg = f"edge_id {item['subject_id']} no longer exists"
            raise LookupError(msg)
        edge, text = loaded
        vector = await embeddings.embed(text, input_type=EMBED_INPUT_TYPE)
        await _upsert_edge_embedding(conn, edge, text, vector, model_id)
    elif item["subject_kind"] == "chunk":
        chunk = await _load_chunk(conn, item["subject_id"])
        if chunk is None:
            msg = f"chunk_id {item['subject_id']} no longer exists"
            raise LookupError(msg)
        vector = await embeddings.embed(chunk["content"], input_type=EMBED_INPUT_TYPE)
        await _write_chunk_embedding(conn, chunk, vector, model_id)
    else:
        # Unreachable while kg.embed_queue's CHECK constraint holds
        # (003_vectors_hnsw.sql allows only node/edge/chunk). Kept so that
        # adding a fourth subject_kind to the constraint without adding a
        # branch here fails on the first queued row instead of dropping it.
        msg = f"unknown embed_queue subject_kind {item['subject_kind']!r}"
        raise ValueError(msg)


async def _process_one(conn: db.Connection, item: dict[str, Any]) -> None:
    queue_id = item["queue_id"]
    try:
        # Nested transaction -> SAVEPOINT: if _embed_and_write fails partway
        # (a bad Bedrock response, a missing row), only this SAVEPOINT rolls
        # back. Without it, a raised psycopg error would abort the WHOLE
        # transaction and the _mark_failed() call below would itself fail
        # with "current transaction is aborted" -- verified empirically.
        async with conn.transaction():
            await _embed_and_write(conn, item)
    except Exception as exc:
        log.exception(
            "embed_failed",
            queue_id=queue_id,
            subject_kind=item["subject_kind"],
            subject_id=item["subject_id"],
        )
        await _mark_failed(conn, queue_id, f"{type(exc).__name__}: {exc}")
        return

    await _mark_complete(conn, queue_id)
    log.info(
        "embedded",
        subject_kind=item["subject_kind"],
        subject_id=item["subject_id"],
        queue_id=queue_id,
        attempt=item["attempts"] + 1,
    )


async def run_once() -> int:
    """Claim and process one batch. Returns the number of rows processed."""
    settings = get_settings().embedder
    async with db.tool_transaction(role=settings.role, statement_timeout="60s") as conn:
        batch = await _claim_batch(conn, settings.batch_size)
    if not batch:
        return 0
    for item in batch:
        # Each item gets its own short transaction so one failure doesn't
        # abort the whole batch's claim-and-commit.
        async with db.tool_transaction(role=settings.role, statement_timeout="60s") as conn:
            await _process_one(conn, item)
    return len(batch)


async def run_forever(stop_event: asyncio.Event) -> None:
    settings = get_settings().embedder
    log.info(
        "embedder_worker_started",
        role=settings.role,
        batch_size=settings.batch_size,
        poll_interval_s=settings.poll_interval_seconds,
        model=get_settings().embedding.model_id,
    )
    while not stop_event.is_set():
        try:
            n = await run_once()
        except Exception:
            log.exception("embedder_batch_failed")
            n = 0
        if n == 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=settings.poll_interval_seconds)
    log.info("embedder_worker_stopping")


def main() -> None:
    configure_logging()
    stop_event = asyncio.Event()

    async def _amain() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # e.g. Windows
                loop.add_signal_handler(sig, stop_event.set)
        try:
            await run_forever(stop_event)
        finally:
            await db.close_pool()

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
