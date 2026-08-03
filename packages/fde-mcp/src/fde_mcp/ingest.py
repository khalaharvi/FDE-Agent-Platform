"""ingest.py -- the evidence-write contract, spelled once for both writers.

Two surfaces now put rows in `kg.source` and `kg.chunk`: the MCP tools an
agent calls (`tools/evidence.py`) and the console's transcript intake
(`fde_gate.service.sources`). They must agree about everything a caller can
observe -- the 8000-character chunk cap, the 200-chunk batch limit, what a
checksum collision does, which ordinals get skipped, and the exact words of
the warning that says a chunk is invisible to retrieval.

Why here and not in fde-gate
-----------------------------
Same reason `playbook.py` lives here: fde-gate depends on fde-mcp and not the
other way round, so shared code can only sit on this side of that edge. The
alternative -- two copies of the INSERT with a comment in each warning the
other not to drift -- is a hazard written down, not a hazard removed. A
`kg.chunk` column added to one copy and forgotten in the other would mean an
agent's ingestion and an operator's ingestion produced different rows from
the same text, and nothing would notice until a search came back short.

Only query TEXT and pure functions live here. Role selection stays at each
call site (`db.tool_transaction(role=...)`), because the two writers run as
genuinely different database roles -- `fde_agent` for the MCP tools (db/014),
`fde_gate_service` for the console (db/017) -- and that difference is a
privilege decision, not a query detail. This module holds no psycopg import
and opens no connection.

The warnings are part of the contract
--------------------------------------
`kg.hybrid_search`'s chunk arm keeps only rows with
`cardinality(anchor_keys) > 0` (db/008_retrieval.sql:317), so a chunk with no
anchors can be stored, embedded, indexed, and completely unreachable. Empty
anchors are still ALLOWED -- a transcript is usually chunked before anyone
knows what it evidences -- so the only defence is saying so, in the same
words, wherever the write happened. The console renders these strings on a
banner; the MCP tool returns them in `warnings`.
"""

from __future__ import annotations

__all__ = [
    "CHUNK_CONTENT_MAX_CHARS",
    "DUPLICATE_SOURCE_NOTE",
    "ENQUEUE_CHUNKS_SQL",
    "INSERT_CHUNKS_SQL",
    "INSERT_SOURCE_SQL",
    "MAX_CHUNKS_PER_CALL",
    "SOURCE_BY_CHECKSUM_SQL",
    "SOURCE_KINDS",
    "UNKNOWN_ANCHORS_SQL",
    "skipped_ordinals_warning",
    "unanchored_warning",
    "unknown_anchors_warning",
]

# kg.chunk.content is unbounded in the schema; the cap here is the embedder's
# budget, not the database's. amazon.titan-embed-text-v2 takes ~8k tokens, and
# capping characters (a strictly tighter bound than tokens) is what lets
# embedder_worker.py embed `content` verbatim with no truncation logic and no
# silent quality cliff when a caller passes a whole document as one chunk.
CHUNK_CONTENT_MAX_CHARS = 8000

# One INSERT ... jsonb_to_recordset per call, so this bounds a statement's
# size rather than a document's. A longer document is more calls, which is
# why the console's intake batches instead of refusing.
MAX_CHUNKS_PER_CALL = 200

# Mirrors kg.source's own CHECK constraint (db/002_graph_core.sql:56-58).
SOURCE_KINDS: tuple[str, ...] = (
    "interview",
    "sop_document",
    "screen_recording",
    "system_export",
    "observation",
    "sme_assertion",
    "sor_telemetry",
    "agent_inference",
)

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# Checksum dedupe. Scoped to the engagement because the same SOP genuinely
# arriving on two engagements is two sources; ORDER BY source_id so a database
# that somehow holds two matches returns the older one, deterministically,
# rather than whichever the planner reached first.
SOURCE_BY_CHECKSUM_SQL = """
    SELECT source_id, source_kind, title, captured_at
      FROM kg.source
     WHERE engagement_id = %(eng)s::uuid AND checksum = %(checksum)s
     ORDER BY source_id
     LIMIT 1
"""

INSERT_SOURCE_SQL = """
    INSERT INTO kg.source
        (engagement_id, source_kind, uri, title, captured_at,
         captured_by, checksum, metadata)
    VALUES (%(eng)s::uuid, %(kind)s, %(uri)s, %(title)s, %(captured_at)s,
            %(captured_by)s, %(checksum)s, %(metadata)s)
    RETURNING source_id, source_kind, title, uri, captured_at, created_at
"""

# ON CONFLICT (source_id, ordinal) DO NOTHING is the whole of chunk
# immutability at this layer -- a re-sent ordinal is reported back as skipped,
# never merged over. db/014 and db/017 grant INSERT and nothing else, so the
# guarantee survives a bug in the code above it.
INSERT_CHUNKS_SQL = """
    INSERT INTO kg.chunk
        (engagement_id, source_id, ordinal, content, token_count,
         anchor_keys, metadata)
    SELECT %(eng)s::uuid, %(sid)s, x.ordinal, x.content, x.token_count,
           COALESCE(x.anchor_keys, '{}'::text[]),
           COALESCE(x.metadata, '{}'::jsonb)
      FROM jsonb_to_recordset(%(chunks)s::jsonb)
        AS x(ordinal int, content text, token_count int,
             anchor_keys text[], metadata jsonb)
    ON CONFLICT (source_id, ordinal) DO NOTHING
    RETURNING chunk_id, ordinal
"""

# ON CONFLICT (subject_kind, subject_id) DO NOTHING: a chunk already queued is
# already queued. Requeuing is not how a re-embed is requested (that is a
# `completed_at = NULL` update by an operator).
ENQUEUE_CHUNKS_SQL = """
    INSERT INTO kg.embed_queue (engagement_id, subject_kind, subject_id)
    SELECT %(eng)s::uuid, 'chunk', c
      FROM unnest(%(ids)s::bigint[]) AS c
    ON CONFLICT (subject_kind, subject_id) DO NOTHING
    RETURNING queue_id
"""

UNKNOWN_ANCHORS_SQL = """
    SELECT t.k AS node_key
      FROM unnest(%(keys)s::text[]) AS t(k)
     WHERE NOT EXISTS (
             SELECT 1 FROM kg.node_current n
              WHERE n.engagement_id = %(eng)s::uuid AND n.node_key = t.k)
"""

# ---------------------------------------------------------------------------
# The words both surfaces say
# ---------------------------------------------------------------------------

DUPLICATE_SOURCE_NOTE = (
    "a source with this checksum already exists for the engagement and was "
    "reused; cite this source_id and check kg_list_sources before re-ingesting "
    "its chunks."
)


def unanchored_warning(ordinals: list[int]) -> str:
    """The invisibility warning, naming the ordinals it is about.

    The most important sentence this platform emits: it is the difference
    between an ingestion that looks successful and yields nothing, and an
    operator who knows to run the extraction agent next.
    """
    return (
        f"chunks {ordinals} have empty anchor_keys and are INVISIBLE to "
        "kg_search's chunk arm: kg.hybrid_search keeps only chunks with "
        "cardinality(anchor_keys) > 0 (db/008_retrieval.sql), so these will be "
        "embedded and stored but can never raise a node's rank. Pass the "
        "node_keys each chunk evidences."
    )


def unknown_anchors_warning(node_keys: list[str]) -> str:
    """Anchors naming nodes that are not live. A warning, never a failure."""
    return (
        f"anchor_keys {node_keys} match no live node in kg.node_current for "
        "this engagement. Not an error -- the node may be in a proposal that "
        "has not merged yet -- but until it exists these anchors contribute "
        "nothing to retrieval."
    )


def skipped_ordinals_warning(ordinals: list[int], source_id: int) -> str:
    """Ordinals the INSERT left alone, because chunks are immutable."""
    return (
        f"ordinals {ordinals} already exist for source_id {source_id} and were "
        "left untouched (chunks are immutable). To re-chunk this document, "
        "register a new source."
    )
