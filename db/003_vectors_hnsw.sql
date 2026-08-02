-- =====================================================================
-- 003_vectors_hnsw.sql
-- Multi-granular embeddings + HNSW indexes
--
-- Three granularities are embedded separately, because a query matches at
-- different levels depending on what it is asking:
--   * node   -- "who approves a discount over 20%?"        -> role/control nodes
--   * edge   -- "what happens after legal review?"          -> precedes/hands_off_to
--   * chunk  -- "what did the RevOps lead say about SLAs?"  -> evidence text
-- Retrieval fuses all three (see 007). Collapsing them into one index is the
-- single most common reason GraphRAG retrieval underperforms.
--
-- INDEX STRATEGY
--   Storage:  vector(1024)  -- full precision, used for exact re-ranking
--   Index:    halfvec(1024) -- 2 bytes/dim instead of 4, ~half the index RAM
--   Filter:   partial indexes WHERE valid_to IS NULL (live rows only)
--
-- The halfvec expression index means queries MUST cast the probe the same
-- way or the planner will not use the index:
--     ORDER BY (embedding::halfvec(1024)) <=> ($1::halfvec(1024))
-- Every query in 007 does this. Do not hand-write a query that forgets it.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Node embeddings
-- ---------------------------------------------------------------------
CREATE TABLE kg.node_embedding (
  node_id       bigint PRIMARY KEY REFERENCES kg.node(node_id) ON DELETE CASCADE,
  engagement_id uuid   NOT NULL,
  node_type     kg.node_type NOT NULL,   -- denormalised for partial indexes
  -- Text that was embedded. Kept so re-embedding on a model change is a
  -- pure recompute with no join back to source.
  embed_text    text   NOT NULL,
  embedding     kg.embedding NOT NULL,
  model_id      text   NOT NULL,          -- e.g. amazon.titan-embed-text-v2:0
  model_dims    int    NOT NULL DEFAULT 1024,
  normalized    boolean NOT NULL DEFAULT true,
  is_current    boolean NOT NULL DEFAULT true,   -- mirrors node.valid_to IS NULL
  embedded_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Edge embeddings -- embed the VERBALISED relation, not the raw type.
-- "Sales Rep hands off Quote Approval to Deal Desk when discount exceeds 20%"
-- retrieves far better than "hands_off_to".
-- ---------------------------------------------------------------------
CREATE TABLE kg.edge_embedding (
  edge_id       bigint PRIMARY KEY REFERENCES kg.edge(edge_id) ON DELETE CASCADE,
  engagement_id uuid   NOT NULL,
  edge_type     kg.edge_type NOT NULL,
  embed_text    text   NOT NULL,
  embedding     kg.embedding NOT NULL,
  model_id      text   NOT NULL,
  is_current    boolean NOT NULL DEFAULT true,
  embedded_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Evidence chunks -- the raw text layer. Anchored to a source and, once
-- extraction has run, to the node/edge keys it supports.
-- ---------------------------------------------------------------------
CREATE TABLE kg.chunk (
  chunk_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id uuid   NOT NULL,
  source_id     bigint NOT NULL REFERENCES kg.source(source_id) ON DELETE CASCADE,
  ordinal       int    NOT NULL,
  content       text   NOT NULL,
  token_count   int,
  -- Keys this chunk was used as evidence for. Lets graph expansion reach
  -- back into text, and text results expand out into the graph.
  anchor_keys   text[] NOT NULL DEFAULT '{}',
  embedding     kg.embedding,
  model_id      text,
  metadata      jsonb  NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_id, ordinal)
);
CREATE INDEX chunk_engagement_idx ON kg.chunk (engagement_id);
CREATE INDEX chunk_anchor_gin     ON kg.chunk USING gin (anchor_keys);
CREATE INDEX chunk_content_trgm   ON kg.chunk USING gin (content gin_trgm_ops);

-- =====================================================================
-- HNSW indexes
--
-- Build parameters. m=16 / ef_construction=64 are the pgvector defaults and
-- are correct for corpora up to ~1M vectors. A single FDE engagement will
-- have 10^3-10^5 nodes; the whole platform across engagements stays well
-- inside that. Raise ef_construction to 128 only if recall@10 measured by
-- the rival grader (see 006) sits below target after tuning ef_search.
--
-- BUILD MEMORY: set maintenance_work_mem large enough that the in-progress
-- graph fits, or pgvector spills and the build slows by an order of
-- magnitude. Rule of thumb: 4-5x the raw vector bytes.
--   1024 dims * 2 bytes (halfvec) * 100k rows ~= 205 MB raw -> ~1 GB index.
--   SET maintenance_work_mem = '2GB';
--   SET max_parallel_maintenance_workers = 7;
-- =====================================================================

-- Per-node-type partial indexes. This is a TRUE pre-filter: the index only
-- contains rows of that type, so a type-scoped query never suffers the
-- post-filter shortfall that plagues a single whole-table HNSW index.
-- Measured elsewhere at ~11x smaller and ~20x faster to build for a type
-- covering ~9% of rows, at equivalent query latency.
--
-- Indexed types are the ones retrieval actually filters on. Types not listed
-- fall back to the catch-all index below.
CREATE INDEX node_emb_hnsw_activity ON kg.node_embedding
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE is_current AND node_type = 'activity';

CREATE INDEX node_emb_hnsw_process ON kg.node_embedding
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE is_current AND node_type = 'process';

CREATE INDEX node_emb_hnsw_system ON kg.node_embedding
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE is_current AND node_type IN ('system','system_object');

CREATE INDEX node_emb_hnsw_control ON kg.node_embedding
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE is_current AND node_type IN ('control','decision');

CREATE INDEX node_emb_hnsw_opportunity ON kg.node_embedding
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE is_current AND node_type IN ('pain_point','opportunity');

-- Catch-all for untyped / cross-type search.
CREATE INDEX node_emb_hnsw_all ON kg.node_embedding
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE is_current;

CREATE INDEX edge_emb_hnsw_all ON kg.edge_embedding
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE is_current;

-- Flow-shaped edges get their own index: "what happens next" queries filter
-- to exactly these three types and they are a small slice of all edges.
CREATE INDEX edge_emb_hnsw_flow ON kg.edge_embedding
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE is_current AND edge_type IN ('precedes','hands_off_to','depends_on');

CREATE INDEX chunk_emb_hnsw ON kg.chunk
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE embedding IS NOT NULL;

-- Scoping indexes used alongside the ANN scan.
CREATE INDEX node_emb_scope_idx  ON kg.node_embedding (engagement_id, node_type) WHERE is_current;
CREATE INDEX edge_emb_scope_idx  ON kg.edge_embedding (engagement_id, edge_type) WHERE is_current;

-- ---------------------------------------------------------------------
-- Keeping is_current honest.
-- kg.node is append-only; when a version is closed out, its embedding must
-- drop out of the HNSW partial index or retrieval will surface stale facts.
-- ---------------------------------------------------------------------
CREATE FUNCTION kg.sync_embedding_currency() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'node' THEN
    UPDATE kg.node_embedding
       SET is_current = (NEW.valid_to IS NULL AND NEW.tx_to IS NULL)
     WHERE node_id = NEW.node_id
       AND is_current <> (NEW.valid_to IS NULL AND NEW.tx_to IS NULL);
  ELSE
    UPDATE kg.edge_embedding
       SET is_current = (NEW.valid_to IS NULL AND NEW.tx_to IS NULL)
     WHERE edge_id = NEW.edge_id
       AND is_current <> (NEW.valid_to IS NULL AND NEW.tx_to IS NULL);
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER node_embedding_currency
  AFTER UPDATE OF valid_to, tx_to ON kg.node
  FOR EACH ROW EXECUTE FUNCTION kg.sync_embedding_currency();

CREATE TRIGGER edge_embedding_currency
  AFTER UPDATE OF valid_to, tx_to ON kg.edge
  FOR EACH ROW EXECUTE FUNCTION kg.sync_embedding_currency();

-- ---------------------------------------------------------------------
-- Embedding backlog. The embedder is an async worker, not part of the
-- merge transaction -- a Bedrock timeout must never block a human approval.
-- ---------------------------------------------------------------------
CREATE TABLE kg.embed_queue (
  queue_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id uuid NOT NULL,
  subject_kind  text NOT NULL CHECK (subject_kind IN ('node','edge','chunk')),
  subject_id    bigint NOT NULL,
  attempts      int NOT NULL DEFAULT 0,
  last_error    text,
  enqueued_at   timestamptz NOT NULL DEFAULT now(),
  claimed_at    timestamptz,
  completed_at  timestamptz,
  UNIQUE (subject_kind, subject_id)
);
CREATE INDEX embed_queue_pending_idx
  ON kg.embed_queue (enqueued_at) WHERE completed_at IS NULL;
