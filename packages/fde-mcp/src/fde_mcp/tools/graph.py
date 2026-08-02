"""tools/graph.py -- read-only knowledge-graph tools.

Every tool here is a thin, typed wrapper over one function in
db/008_retrieval.sql: `kg_search` calls `kg.hybrid_search`, `kg_traverse`
calls `kg.traverse`, and so on. None of the retrieval, ranking, or
bounding logic is reimplemented in Python -- see that file's module
docstring for why production inference and RL rollout must see
byte-identical retrieval behaviour, which is only true if there is exactly
one implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from fde_mcp import db, embeddings
from fde_mcp.tools._base import (
    EdgeType,
    NodeType,
    emit_trace,
    fetchall,
    fetchone,
    now_ms,
    parse_timestamp,
    pg_error_boundary,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["register"]

_vector_literal = embeddings.to_pgvector_literal


@pg_error_boundary
async def kg_head_commit(engagement_id: str) -> dict[str, Any]:
    """Return the current sealed HEAD commit for this engagement.

    Call this FIRST, before any other kg_* tool, and pin the returned
    `commit_id` for the rest of the task: cite it in proposals
    (`base_commit_id`) and in workflow drafts (`pinned_commit_id`) so the
    graph state you reasoned over is reproducible. If the graph has never
    been merged into, `commit_id` is null and `has_head` is false -- there
    is nothing to query yet.
    """
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT commit_id, commit_uuid, title, content_digest,
                       sealed_by, sealed_at, created_at
                  FROM kg.commit
                 WHERE engagement_id = %(eng)s::uuid AND status = 'sealed'
                 ORDER BY commit_id DESC
                 LIMIT 1
                """,
                {"eng": engagement_id},
            )
            row = await fetchone(cur)
        result = {"has_head": row is not None, **(row or {"commit_id": None})}
        await emit_trace(conn, "kg_head_commit", result)
        return result


@pg_error_boundary
async def kg_as_of(engagement_id: str, at: str) -> dict[str, Any]:
    """Point-in-time snapshot summary for `at` (ISO-8601 timestamp), diffed
    against the current live graph.

    Use this to answer "what did the graph say on date X" or "what changed
    since X" questions. Returns node/edge counts as of `at`, the current
    HEAD commit, current counts, and a bounded diff (added/removed keys,
    capped at 200 each with total counts) between the two. `at` is
    interpreted as both the valid-time and transaction-time cutoff (i.e.
    "the graph's current best belief about what was true at `at`").
    """
    t0 = now_ms()
    at_ts = parse_timestamp(at)
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT node_key FROM kg.node_as_of(%(eng)s::uuid, %(at)s)",
                {"eng": engagement_id, "at": at_ts},
            )
            asof_nodes = {r["node_key"] for r in await fetchall(cur)}

            await cur.execute(
                "SELECT edge_key FROM kg.edge_as_of(%(eng)s::uuid, %(at)s)",
                {"eng": engagement_id, "at": at_ts},
            )
            asof_edges = {r["edge_key"] for r in await fetchall(cur)}

            await cur.execute(
                "SELECT node_key FROM kg.node_current WHERE engagement_id = %(eng)s::uuid",
                {"eng": engagement_id},
            )
            head_nodes = {r["node_key"] for r in await fetchall(cur)}

            await cur.execute(
                "SELECT edge_key FROM kg.edge_current WHERE engagement_id = %(eng)s::uuid",
                {"eng": engagement_id},
            )
            head_edges = {r["edge_key"] for r in await fetchall(cur)}

            await cur.execute(
                """
                SELECT commit_id, content_digest, sealed_at
                  FROM kg.commit
                 WHERE engagement_id = %(eng)s::uuid AND status = 'sealed'
                 ORDER BY commit_id DESC LIMIT 1
                """,
                {"eng": engagement_id},
            )
            head = await fetchone(cur)

        nodes_added = sorted(head_nodes - asof_nodes)
        nodes_removed = sorted(asof_nodes - head_nodes)
        edges_added = sorted(head_edges - asof_edges)
        edges_removed = sorted(asof_edges - head_edges)

        result = {
            "engagement_id": engagement_id,
            "as_of": at,
            "node_count_as_of": len(asof_nodes),
            "edge_count_as_of": len(asof_edges),
            "head": head,
            "node_count_head": len(head_nodes),
            "edge_count_head": len(head_edges),
            "diff_vs_head": {
                "nodes_added_since_count": len(nodes_added),
                "nodes_removed_since_count": len(nodes_removed),
                "edges_added_since_count": len(edges_added),
                "edges_removed_since_count": len(edges_removed),
                "nodes_added_since": nodes_added[:200],
                "nodes_removed_since": nodes_removed[:200],
                "edges_added_since": edges_added[:200],
                "edges_removed_since": edges_removed[:200],
            },
        }
        await emit_trace(conn, "kg_as_of", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def kg_search(
    engagement_id: str,
    query: str,
    k: Annotated[int, Field(ge=1, le=200)] = 20,
    node_types: list[NodeType] | None = None,
    expand_hops: Annotated[int, Field(ge=0, le=6)] = 2,
) -> dict[str, Any]:
    """Hybrid ANN + graph retrieval over the knowledge graph (kg.hybrid_search).

    Embeds `query`, fuses (Reciprocal Rank Fusion, k=60) four ranked lists --
    node ANN, edge ANN (via endpoints), evidence-chunk ANN, and bounded graph
    expansion from the top node seeds -- and returns up to `k` nodes.

    THIS IS THE PRIMARY RETRIEVAL TOOL. Use it for open-ended "what/who/how"
    questions about the business. Use kg_lexical_search instead (or in
    addition) when the question uses exact internal jargon an embedding
    model won't know (ticket-system codes, internal acronyms).

    Every returned row carries a `provenance` object: which ranked list(s)
    it matched (node_ann/edge_ann/chunk_ann/graph_expand) and at what rank,
    plus `lists_matched` and `evidence_strength`. YOU MUST CITE provenance
    for any claim you make from these results -- a result with only
    `graph_expand` provenance and low evidence_strength is a much weaker
    basis for an assertion than one matched by multiple lists with high
    evidence_strength, and the grading/RL reward pipeline checks for this.
    """
    t0 = now_ms()
    vec = await embeddings.embed(query, input_type="search_query")
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT node_key, node_type, label, summary, rrf_score, provenance
                  FROM kg.hybrid_search(
                    %(eng)s::uuid, %(vec)s::kg.embedding, %(k)s, %(seed_k)s,
                    %(hops)s, NULL::kg.edge_type[], %(node_types)s::kg.node_type[], 60
                  )
                """,
                {
                    "eng": engagement_id,
                    "vec": _vector_literal(vec),
                    "k": k,
                    "seed_k": max(30, k),
                    "hops": expand_hops,
                    "node_types": node_types,
                },
            )
            rows = await fetchall(cur)
        result = {
            "engagement_id": engagement_id,
            "query": query,
            "k": k,
            "returned": len(rows),
            "results": rows,
        }
        latency_ms = int(now_ms() - t0)
        rrf_top = rows[0]["rrf_score"] if rows else None
        await emit_trace(
            conn,
            "kg_search",
            result,
            retrieval={
                "k": k,
                "returned": len(rows),
                "rrf_top": rrf_top,
                "hops": expand_hops,
                "latency_ms": latency_ms,
            },
            latency_ms=latency_ms,
        )
        return result


@pg_error_boundary
async def kg_lexical_search(
    engagement_id: str, text: str, k: Annotated[int, Field(ge=1, le=100)] = 10
) -> dict[str, Any]:
    """Trigram/lexical fallback over node labels (kg.lexical_search).

    Use this WHEN kg_search comes back empty or low-confidence AND the query
    contains exact internal jargon an embedding model has likely never seen
    (an internal system code like "CPQ-3", a ticket-system status string, an
    acronym coined inside the engagement). It matches on label similarity,
    not semantics -- it will not find a conceptually related but
    differently-worded node. Returns node_key/node_type/label/sim, no
    provenance object (nothing to fuse against); cite the node_key directly.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT node_key, node_type, label, sim FROM kg.lexical_search(%(eng)s::uuid, %(text)s, %(k)s)",
                {"eng": engagement_id, "text": text, "k": k},
            )
            rows = await fetchall(cur)
        result = {
            "engagement_id": engagement_id,
            "text": text,
            "k": k,
            "returned": len(rows),
            "results": rows,
        }
        latency_ms = int(now_ms() - t0)
        await emit_trace(
            conn,
            "kg_lexical_search",
            result,
            retrieval={
                "k": k,
                "returned": len(rows),
                "rrf_top": None,
                "hops": None,
                "latency_ms": latency_ms,
            },
            latency_ms=latency_ms,
        )
        return result


@pg_error_boundary
async def kg_get_node(engagement_id: str, node_key: str) -> dict[str, Any]:
    """Fetch one live node plus all its live incident edges and evidence.

    Returns the node row, its outgoing and incoming edges grouped by
    edge_type (each entry has the neighbour key/label plus the edge's own
    label/attributes/confidence/human_confirmed), and the kg.evidence rows
    (joined to kg.source for title/kind/uri) that back the node itself, plus
    an aggregate `evidence_strength`. Use this to go deep on one entity
    after kg_search or kg_traverse has surfaced its node_key; it is the
    right tool for "tell me everything about X" rather than a broad search.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM kg.node_current WHERE engagement_id = %(eng)s::uuid AND node_key = %(key)s",
                {"eng": engagement_id, "key": node_key},
            )
            node = await fetchone(cur)
            if node is None:
                result: dict[str, Any] = {
                    "error": "node not found",
                    "hint": "check node_key and that it is currently live (not retired/superseded)",
                }
                await emit_trace(conn, "kg_get_node", result, latency_ms=int(now_ms() - t0))
                return result

            await cur.execute(
                """
                SELECT e.edge_type, e.edge_key, e.dst_key AS neighbor_key, n.label AS neighbor_label,
                       e.label, e.attributes, e.weight, e.confidence, e.human_confirmed
                  FROM kg.edge_current e
                  JOIN kg.node_current n ON n.engagement_id = e.engagement_id AND n.node_key = e.dst_key
                 WHERE e.engagement_id = %(eng)s::uuid AND e.src_key = %(key)s
                """,
                {"eng": engagement_id, "key": node_key},
            )
            out_rows = await fetchall(cur)

            await cur.execute(
                """
                SELECT e.edge_type, e.edge_key, e.src_key AS neighbor_key, n.label AS neighbor_label,
                       e.label, e.attributes, e.weight, e.confidence, e.human_confirmed
                  FROM kg.edge_current e
                  JOIN kg.node_current n ON n.engagement_id = e.engagement_id AND n.node_key = e.src_key
                 WHERE e.engagement_id = %(eng)s::uuid AND e.dst_key = %(key)s
                """,
                {"eng": engagement_id, "key": node_key},
            )
            in_rows = await fetchall(cur)

            await cur.execute(
                """
                SELECT ev.evidence_id, ev.excerpt, ev.locator, ev.extraction_method,
                       ev.confidence, ev.extracted_at,
                       s.source_id, s.source_kind, s.title, s.uri, s.captured_at
                  FROM kg.evidence ev
                  JOIN kg.source s ON s.source_id = ev.source_id
                 WHERE ev.engagement_id = %(eng)s::uuid AND ev.subject_kind = 'node' AND ev.subject_key = %(key)s
                 ORDER BY ev.extracted_at DESC
                """,
                {"eng": engagement_id, "key": node_key},
            )
            evidence = await fetchall(cur)

            await cur.execute(
                "SELECT kg.evidence_strength(%(eng)s::uuid, 'node', %(key)s) AS strength",
                {"eng": engagement_id, "key": node_key},
            )
            strength_row = await fetchone(cur)
            strength = strength_row["strength"] if strength_row is not None else None

        def _group(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                grouped.setdefault(r["edge_type"], []).append(r)
            return grouped

        result = {
            "node": node,
            "edges": {"outgoing": _group(out_rows), "incoming": _group(in_rows)},
            "evidence": evidence,
            "evidence_strength": strength,
        }
        await emit_trace(conn, "kg_get_node", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def kg_traverse(
    engagement_id: str,
    start_keys: list[str],
    *,
    edge_types: list[EdgeType] | None = None,
    max_hops: Annotated[int, Field(ge=1, le=6)] = 3,
    max_nodes: Annotated[int, Field(ge=1, le=5000)] = 500,
    direction: Literal["out", "in", "both"] = "out",
) -> dict[str, Any]:
    """Bounded, cycle-safe multi-hop traversal from one or more start nodes
    (kg.traverse).

    Three cost bounds are ALWAYS enforced, by both this tool's own schema
    and the database: max_hops <= 6, max_nodes <= 5000, edge_types
    (narrowing branching factor). There is no unbounded mode -- if you need
    more, run kg_traverse again from the frontier it returned. `direction`
    controls which way edges are followed: 'out' (dependencies/downstream),
    'in' (what points at these nodes/upstream), or 'both'. Returns each
    reachable node once, at its shortest hop-count with the highest-
    confidence path to it (node_key, node_type, label, summary, depth, path,
    via_edge_key, via_edge_type, path_confidence).
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT node_key, node_type, label, summary, depth, path,
                       via_edge_key, via_edge_type, path_confidence
                  FROM kg.traverse(
                    %(eng)s::uuid, %(start_keys)s, %(edge_types)s::kg.edge_type[],
                    %(max_hops)s, %(max_nodes)s, %(direction)s
                  )
                """,
                {
                    "eng": engagement_id,
                    "start_keys": start_keys,
                    "edge_types": edge_types,
                    "max_hops": max_hops,
                    "max_nodes": max_nodes,
                    "direction": direction,
                },
            )
            rows = await fetchall(cur)
        result = {
            "engagement_id": engagement_id,
            "start_keys": start_keys,
            "max_hops": max_hops,
            "max_nodes": max_nodes,
            "direction": direction,
            "returned": len(rows),
            "nodes": rows,
        }
        latency_ms = int(now_ms() - t0)
        await emit_trace(
            conn,
            "kg_traverse",
            result,
            retrieval={
                "k": max_nodes,
                "returned": len(rows),
                "rrf_top": None,
                "hops": max_hops,
                "latency_ms": latency_ms,
            },
            latency_ms=latency_ms,
        )
        return result


@pg_error_boundary
async def kg_dependency_closure(
    engagement_id: str, node_key: str, max_hops: Annotated[int, Field(ge=1, le=6)] = 4
) -> dict[str, Any]:
    """ "What does this activity actually depend on, transitively?"
    (kg.dependency_closure). Follows depends_on/consumes/recorded_in/
    gated_by edges outward. This is the tool to answer "what breaks if I
    automate/remove X" from the dependency side -- for the reverse question
    ("what depends on X"), use kg_impact_radius instead.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT node_key, node_type, label, depth, path, path_confidence "
                "FROM kg.dependency_closure(%(eng)s::uuid, %(key)s, %(hops)s)",
                {"eng": engagement_id, "key": node_key, "hops": max_hops},
            )
            rows = await fetchall(cur)
        result = {
            "engagement_id": engagement_id,
            "node_key": node_key,
            "max_hops": max_hops,
            "returned": len(rows),
            "closure": rows,
        }
        latency_ms = int(now_ms() - t0)
        await emit_trace(
            conn,
            "kg_dependency_closure",
            result,
            retrieval={
                "k": None,
                "returned": len(rows),
                "rrf_top": None,
                "hops": max_hops,
                "latency_ms": latency_ms,
            },
            latency_ms=latency_ms,
        )
        return result


@pg_error_boundary
async def kg_impact_radius(
    engagement_id: str, node_key: str, max_hops: Annotated[int, Field(ge=1, le=6)] = 4
) -> dict[str, Any]:
    """ "If this system/control/activity changes or goes away, what is
    affected?" (kg.impact_radius). Follows depends_on/consumes/recorded_in/
    gated_by/precedes/hands_off_to edges INWARD (reverse of
    kg_dependency_closure). Use this before proposing a retire_node/
    retire_edge, or when scoping the blast radius of a proposed automation.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT node_key, node_type, label, depth, path, path_confidence "
                "FROM kg.impact_radius(%(eng)s::uuid, %(key)s, %(hops)s)",
                {"eng": engagement_id, "key": node_key, "hops": max_hops},
            )
            rows = await fetchall(cur)
        result = {
            "engagement_id": engagement_id,
            "node_key": node_key,
            "max_hops": max_hops,
            "returned": len(rows),
            "impact": rows,
        }
        latency_ms = int(now_ms() - t0)
        await emit_trace(
            conn,
            "kg_impact_radius",
            result,
            retrieval={
                "k": None,
                "returned": len(rows),
                "rrf_top": None,
                "hops": max_hops,
                "latency_ms": latency_ms,
            },
            latency_ms=latency_ms,
        )
        return result


@pg_error_boundary
async def kg_process_flow(engagement_id: str, process_key: str) -> dict[str, Any]:
    """Ordered activity sequence for a process (kg.process_flow).

    Returns each activity belonging to `process_key`, topologically ordered
    by in-degree over `precedes` edges within the set, with performed_by
    (roles), gated_by (controls/decisions), records_to (system_objects), and
    next_keys (what follows, via precedes/hands_off_to). Use this to explain
    or diagram an end-to-end process; use kg_traverse if you need to follow
    edges beyond a single process's activity set.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM kg.process_flow(%(eng)s::uuid, %(key)s)",
                {"eng": engagement_id, "key": process_key},
            )
            rows = await fetchall(cur)
        result = {"engagement_id": engagement_id, "process_key": process_key, "steps": rows}
        await emit_trace(conn, "kg_process_flow", result, latency_ms=int(now_ms() - t0))
        return result


def register(mcp: FastMCP[None]) -> None:
    """Register every read-only graph tool on `mcp`."""
    mcp.tool()(kg_head_commit)
    mcp.tool()(kg_as_of)
    mcp.tool()(kg_search)
    mcp.tool()(kg_lexical_search)
    mcp.tool()(kg_get_node)
    mcp.tool()(kg_traverse)
    mcp.tool()(kg_dependency_closure)
    mcp.tool()(kg_impact_radius)
    mcp.tool()(kg_process_flow)
