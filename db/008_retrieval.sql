-- =====================================================================
-- 008_retrieval.sql
-- Graph traversal + hybrid ANN/graph retrieval. These functions ARE the
-- MCP tool surface -- the MCP server in mcp/ is a thin typed wrapper over
-- exactly these, so the retrieval logic lives in one place, is testable
-- with psql, and is identical between production inference and RL rollout.
--
-- That last point is not incidental. If the retrieval an RL rollout sees
-- differs from what production serves, the policy you train is optimising a
-- different environment than the one it will be deployed into.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Session tuning. Call once per connection in the MCP server's pool
-- initialiser. relaxed_order is the right default: results come back
-- approximately distance-ordered but the filter shortfall is gone, and the
-- fusion step in kg.hybrid_search re-ranks anyway.
-- ---------------------------------------------------------------------
CREATE FUNCTION kg.tune_session(p_ef_search int DEFAULT 100,
                                p_iterative text DEFAULT 'relaxed_order',
                                p_max_scan int DEFAULT 20000)
RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  -- ef_search band 40-200. Above ~200 the planner starts abandoning the HNSW
  -- index for a sequential scan on filtered queries, which is a latency cliff
  -- (observed elsewhere: 2.5ms -> 365ms), not a gradual degradation.
  IF p_ef_search < 40 OR p_ef_search > 200 THEN
    RAISE EXCEPTION 'hnsw.ef_search % outside safe band 40..200', p_ef_search;
  END IF;
  EXECUTE format('SET hnsw.ef_search = %s', p_ef_search);
  EXECUTE format('SET hnsw.iterative_scan = %L', p_iterative);
  EXECUTE format('SET hnsw.max_scan_tuples = %s', p_max_scan);
END $$;

-- =====================================================================
-- 1. TRAVERSAL
-- =====================================================================

-- Bounded multi-hop traversal with cycle detection.
--
-- Three independent cost bounds, all mandatory:
--   max_hops       -- depth ceiling
--   max_nodes      -- total frontier ceiling (the one that saves you on a
--                     high-fanout hub node; depth alone does not)
--   edge_types     -- branching-factor pruning; NULL means all types
--
-- A recursive CTE re-evaluates the recursive term against the whole working
-- table each iteration with no automatic pruning, so an unbounded traversal
-- over a graph with a few hub nodes blows up combinatorially. All three
-- bounds are enforced; there is no "unlimited" mode by design.
CREATE FUNCTION kg.traverse(
  p_engagement  uuid,
  p_start_keys  text[],
  p_edge_types  kg.edge_type[] DEFAULT NULL,
  p_max_hops    int DEFAULT 3,
  p_max_nodes   int DEFAULT 500,
  p_direction   text DEFAULT 'out',           -- 'out' | 'in' | 'both'
  p_min_confidence real DEFAULT 0.0,
  p_as_of       timestamptz DEFAULT NULL      -- NULL = current
)
RETURNS TABLE (
  node_key text, node_type kg.node_type, label text, summary text,
  depth int, path text[], via_edge_key text, via_edge_type kg.edge_type,
  path_confidence real
)
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
DECLARE v_at timestamptz := coalesce(p_as_of, now());
BEGIN
  IF p_max_hops > 6 THEN
    RAISE EXCEPTION 'max_hops % exceeds the hard ceiling of 6', p_max_hops;
  END IF;
  IF p_max_nodes > 5000 THEN
    RAISE EXCEPTION 'max_nodes % exceeds the hard ceiling of 5000', p_max_nodes;
  END IF;

  RETURN QUERY
  WITH RECURSIVE
  live_edge AS (
    SELECT e.edge_key, e.src_key, e.dst_key, e.edge_type, e.confidence, e.weight
      FROM kg.edge e
     WHERE e.engagement_id = p_engagement
       AND e.valid_from <= v_at AND (e.valid_to IS NULL OR e.valid_to > v_at)
       AND e.tx_to IS NULL
       AND e.confidence >= p_min_confidence
       AND (p_edge_types IS NULL OR e.edge_type = ANY(p_edge_types))
  ),
  walk AS (
    SELECT k          AS nkey,
           0          AS depth,
           ARRAY[k]   AS path,
           NULL::text AS via_key,
           NULL::kg.edge_type AS via_type,
           1.0::real  AS pconf
      FROM unnest(p_start_keys) AS k
    UNION ALL
    SELECT nxt.nkey, w.depth + 1, w.path || nxt.nkey, nxt.ekey, nxt.etype,
           (w.pconf * nxt.econf)::real
      FROM walk w
      CROSS JOIN LATERAL (
        SELECT e.dst_key AS nkey, e.edge_key AS ekey, e.edge_type AS etype,
               e.confidence AS econf
          FROM live_edge e
         WHERE p_direction IN ('out','both') AND e.src_key = w.nkey
        UNION ALL
        SELECT e.src_key, e.edge_key, e.edge_type, e.confidence
          FROM live_edge e
         WHERE p_direction IN ('in','both')  AND e.dst_key = w.nkey
      ) nxt
     WHERE w.depth < p_max_hops
  ) CYCLE nkey SET is_cycle USING cyc_path
  SELECT DISTINCT ON (w.nkey)
         w.nkey, n.node_type, n.label, n.summary,
         w.depth, w.path, w.via_key, w.via_type, w.pconf
    FROM walk w
    JOIN kg.node n
      ON n.engagement_id = p_engagement AND n.node_key = w.nkey
     AND n.valid_from <= v_at AND (n.valid_to IS NULL OR n.valid_to > v_at)
     AND n.tx_to IS NULL
   WHERE NOT w.is_cycle
   ORDER BY w.nkey, w.depth, w.pconf DESC
   LIMIT p_max_nodes;
END $$;

COMMENT ON FUNCTION kg.traverse IS
  'Bounded, cycle-safe multi-hop traversal. Uses the PG14+ CYCLE clause. '
  'Three mandatory bounds (hops, nodes, edge types). Returns the shortest '
  'highest-confidence path to each reachable node.';

-- Dependency closure -- the query this platform exists to answer.
-- "What does this activity actually depend on, transitively, and what breaks
--  if I automate it?"
CREATE FUNCTION kg.dependency_closure(
  p_engagement uuid, p_key text, p_max_hops int DEFAULT 4)
RETURNS TABLE (node_key text, node_type kg.node_type, label text,
               depth int, path text[], path_confidence real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT t.node_key, t.node_type, t.label, t.depth, t.path, t.path_confidence
    FROM kg.traverse(p_engagement, ARRAY[p_key],
                     ARRAY['depends_on','consumes','recorded_in','gated_by']::kg.edge_type[],
                     p_max_hops, 1000, 'out') t
   ORDER BY t.depth, t.path_confidence DESC;
$$;

-- Reverse: "if this system goes away / this control changes, what is affected?"
CREATE FUNCTION kg.impact_radius(
  p_engagement uuid, p_key text, p_max_hops int DEFAULT 4)
RETURNS TABLE (node_key text, node_type kg.node_type, label text,
               depth int, path text[], path_confidence real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT t.node_key, t.node_type, t.label, t.depth, t.path, t.path_confidence
    FROM kg.traverse(p_engagement, ARRAY[p_key],
                     ARRAY['depends_on','consumes','recorded_in','gated_by',
                           'precedes','hands_off_to']::kg.edge_type[],
                     p_max_hops, 1000, 'in') t
   ORDER BY t.depth, t.path_confidence DESC;
$$;

-- Process walk: ordered activity sequence for a process, following control flow.
CREATE FUNCTION kg.process_flow(p_engagement uuid, p_process_key text)
RETURNS TABLE (ordinal int, activity_key text, label text,
               performed_by text[], gated_by text[], records_to text[],
               next_keys text[])
LANGUAGE sql STABLE AS $$
  WITH acts AS (
    SELECT n.node_key, n.label
      FROM kg.node_current n
      JOIN kg.edge_current e
        ON e.engagement_id = n.engagement_id AND e.src_key = n.node_key
       AND e.edge_type = 'belongs_to' AND e.dst_key = p_process_key
     WHERE n.engagement_id = p_engagement AND n.node_type = 'activity'
  ),
  ordered AS (
    SELECT a.node_key, a.label,
           -- Topological-ish ordering by in-degree over `precedes` within the set.
           (SELECT count(*) FROM kg.edge_current e
             WHERE e.engagement_id = p_engagement AND e.edge_type = 'precedes'
               AND e.dst_key = a.node_key
               AND e.src_key IN (SELECT node_key FROM acts)) AS indeg
      FROM acts a
  )
  SELECT (row_number() OVER (ORDER BY o.indeg, o.node_key))::int,
         o.node_key, o.label,
         ARRAY(SELECT e.src_key FROM kg.edge_current e
                WHERE e.engagement_id = p_engagement AND e.edge_type = 'performs'
                  AND e.dst_key = o.node_key),
         ARRAY(SELECT e.dst_key FROM kg.edge_current e
                WHERE e.engagement_id = p_engagement AND e.edge_type = 'gated_by'
                  AND e.src_key = o.node_key),
         ARRAY(SELECT e.dst_key FROM kg.edge_current e
                WHERE e.engagement_id = p_engagement AND e.edge_type = 'recorded_in'
                  AND e.src_key = o.node_key),
         ARRAY(SELECT e.dst_key FROM kg.edge_current e
                WHERE e.engagement_id = p_engagement
                  AND e.edge_type IN ('precedes','hands_off_to')
                  AND e.src_key = o.node_key)
    FROM ordered o
   ORDER BY o.indeg, o.node_key;
$$;

-- =====================================================================
-- 2. HYBRID RETRIEVAL
-- ANN seed -> graph expansion -> reciprocal rank fusion.
-- =====================================================================

-- ANN over nodes. Note the double halfvec cast: the index is an expression
-- index on (embedding::halfvec(1024)), so the probe must be cast identically
-- or the planner ignores it and you silently get a sequential scan.
CREATE FUNCTION kg.ann_nodes(
  p_engagement uuid, p_query kg.embedding, p_k int DEFAULT 30,
  p_node_types kg.node_type[] DEFAULT NULL)
RETURNS TABLE (node_key text, node_type kg.node_type, label text,
               summary text, distance real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT n.node_key, n.node_type, n.label, n.summary,
         (ne.embedding::halfvec(1024) <=> p_query::halfvec(1024))::real
    FROM kg.node_embedding ne
    JOIN kg.node n ON n.node_id = ne.node_id
   WHERE ne.engagement_id = p_engagement
     AND ne.is_current
     AND (p_node_types IS NULL OR ne.node_type = ANY(p_node_types))
   ORDER BY ne.embedding::halfvec(1024) <=> p_query::halfvec(1024)
   LIMIT p_k;
$$;

CREATE FUNCTION kg.ann_edges(
  p_engagement uuid, p_query kg.embedding, p_k int DEFAULT 30,
  p_edge_types kg.edge_type[] DEFAULT NULL)
RETURNS TABLE (edge_key text, edge_type kg.edge_type, src_key text,
               dst_key text, verbalisation text, distance real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT e.edge_key, e.edge_type, e.src_key, e.dst_key, ee.embed_text,
         (ee.embedding::halfvec(1024) <=> p_query::halfvec(1024))::real
    FROM kg.edge_embedding ee
    JOIN kg.edge e ON e.edge_id = ee.edge_id
   WHERE ee.engagement_id = p_engagement
     AND ee.is_current
     AND (p_edge_types IS NULL OR ee.edge_type = ANY(p_edge_types))
   ORDER BY ee.embedding::halfvec(1024) <=> p_query::halfvec(1024)
   LIMIT p_k;
$$;

CREATE FUNCTION kg.ann_chunks(
  p_engagement uuid, p_query kg.embedding, p_k int DEFAULT 30)
RETURNS TABLE (chunk_id bigint, source_id bigint, content text,
               anchor_keys text[], distance real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT c.chunk_id, c.source_id, c.content, c.anchor_keys,
         (c.embedding::halfvec(1024) <=> p_query::halfvec(1024))::real
    FROM kg.chunk c
   WHERE c.engagement_id = p_engagement AND c.embedding IS NOT NULL
   ORDER BY c.embedding::halfvec(1024) <=> p_query::halfvec(1024)
   LIMIT p_k;
$$;

-- ---------------------------------------------------------------------
-- kg.hybrid_search -- the primary retrieval entry point.
--
-- Pipeline:
--   1. ANN seed at three granularities (node, edge, chunk), in parallel.
--   2. Graph expansion from node seeds, bounded.
--   3. Reciprocal Rank Fusion across the four ranked lists with k=60.
--
-- RRF with k=60 is the standard Cormack constant and is what the GraphRAG
-- literature uses. It is deliberately rank-based rather than score-based:
-- cosine distances from an ANN index and hop-distances from a traversal are
-- not on a comparable scale, and normalising them invents a calibration you
-- do not have. Ranks are comparable by construction.
--
-- Every returned row carries `provenance` -- the exact list(s) and rank(s)
-- it came from. The Workflow Agent is required to cite this; the rival
-- grader scores against it; the RL reward function reads it to compute
-- path-groundedness.
-- ---------------------------------------------------------------------
CREATE FUNCTION kg.hybrid_search(
  p_engagement   uuid,
  p_query        kg.embedding,
  p_k            int DEFAULT 20,
  p_seed_k       int DEFAULT 30,
  p_expand_hops  int DEFAULT 2,
  p_expand_types kg.edge_type[] DEFAULT NULL,
  p_node_types   kg.node_type[] DEFAULT NULL,
  p_rrf_k        int DEFAULT 60
)
RETURNS TABLE (
  node_key text, node_type kg.node_type, label text, summary text,
  rrf_score real, provenance jsonb
)
LANGUAGE sql STABLE AS $$
  WITH
  -- List A: direct node ANN
  a AS (
    SELECT node_key, row_number() OVER (ORDER BY distance) AS rnk, distance
      FROM kg.ann_nodes(p_engagement, p_query, p_seed_k, p_node_types)
  ),
  -- List B: nodes reached via edge ANN (an edge match implies both endpoints)
  b_raw AS (
    SELECT edge_key, src_key, dst_key,
           row_number() OVER (ORDER BY distance) AS rnk, distance
      FROM kg.ann_edges(p_engagement, p_query, p_seed_k, NULL)
  ),
  b AS (
    SELECT node_key, min(rnk) AS rnk
      FROM (SELECT src_key AS node_key, rnk FROM b_raw
            UNION ALL
            SELECT dst_key, rnk FROM b_raw) x
     GROUP BY node_key
  ),
  -- List C: nodes anchored by chunk ANN
  c_raw AS (
    SELECT chunk_id, anchor_keys,
           row_number() OVER (ORDER BY distance) AS rnk
      FROM kg.ann_chunks(p_engagement, p_query, p_seed_k)
  ),
  c AS (
    SELECT unnest(anchor_keys) AS node_key, min(rnk) AS rnk
      FROM c_raw WHERE cardinality(anchor_keys) > 0
     GROUP BY 1
  ),
  -- List D: graph expansion from the top node seeds. Ranked by (depth,
  -- inverse path confidence) so a 1-hop certain neighbour outranks a 3-hop
  -- speculative one.
  seeds AS (
    SELECT array_agg(node_key) AS keys
      FROM (SELECT node_key FROM a ORDER BY rnk LIMIT 10) s
  ),
  d AS (
    SELECT t.node_key,
           row_number() OVER (ORDER BY t.depth, t.path_confidence DESC) AS rnk
      FROM seeds, LATERAL kg.traverse(p_engagement, seeds.keys, p_expand_types,
                                      p_expand_hops, 300, 'both') t
     WHERE t.depth > 0
  ),
  fused AS (
    SELECT k.node_key,
           sum(1.0 / (p_rrf_k + k.rnk))::real AS score,
           jsonb_object_agg(k.list, jsonb_build_object('rank', k.rnk)) AS prov
      FROM (
        SELECT node_key, rnk, 'node_ann'  AS list FROM a
        UNION ALL SELECT node_key, rnk, 'edge_ann'  FROM b
        UNION ALL SELECT node_key, rnk, 'chunk_ann' FROM c
        UNION ALL SELECT node_key, rnk, 'graph_expand' FROM d
      ) k
     GROUP BY k.node_key
  )
  SELECT f.node_key, n.node_type, n.label, n.summary, f.score,
         f.prov || jsonb_build_object(
           'lists_matched', (SELECT count(*) FROM jsonb_object_keys(f.prov)),
           'evidence_strength', kg.evidence_strength(p_engagement, 'node', f.node_key))
    FROM fused f
    JOIN kg.node_current n
      ON n.engagement_id = p_engagement AND n.node_key = f.node_key
   WHERE (p_node_types IS NULL OR n.node_type = ANY(p_node_types))
   ORDER BY f.score DESC
   LIMIT p_k;
$$;

COMMENT ON FUNCTION kg.hybrid_search IS
  'Primary retrieval entry point. RRF(k=60) over four ranked lists: node ANN, '
  'edge ANN, chunk ANN, graph expansion. Returns per-result provenance so '
  'downstream citation, grading, and RL reward can all verify grounding.';

-- Lexical fallback. When the ANN misses because the user used the exact
-- internal jargon that the embedding model has never seen ("CPQ-3 rework
-- loop"), trigram match on labels finds it. Cheap; always worth running.
CREATE FUNCTION kg.lexical_search(
  p_engagement uuid, p_text text, p_k int DEFAULT 10)
RETURNS TABLE (node_key text, node_type kg.node_type, label text, sim real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT n.node_key, n.node_type, n.label, similarity(n.label, p_text)
    FROM kg.node_current n
   WHERE n.engagement_id = p_engagement
     AND n.label %> p_text
   ORDER BY similarity(n.label, p_text) DESC
   LIMIT p_k;
$$;
