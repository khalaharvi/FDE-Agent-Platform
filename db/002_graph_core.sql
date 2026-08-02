-- =====================================================================
-- 002_graph_core.sql
-- Bitemporal node/edge store + commit log + provenance
--
-- Design contract:
--   * Nothing in kg.node / kg.edge is ever UPDATEd or DELETEd in place.
--     A change closes the current row (valid_to := now()) and inserts a new
--     one. This gives "what did the graph say on date X" for free and makes
--     every workflow reproducible against the commit it was authored from.
--   * Every mutation carries a commit_id. Commits are created only by the
--     HITL merge path (see 004). Agents cannot write here directly --
--     the only GRANT they get is SELECT.
--   * Every edge carries provenance. An edge with no evidence cannot be
--     merged (enforced in 007's promotion function).
-- =====================================================================

-- ---------------------------------------------------------------------
-- Commit log -- the version spine of the whole platform
-- ---------------------------------------------------------------------
CREATE TABLE kg.commit (
  commit_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  commit_uuid    uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  parent_id      bigint      REFERENCES kg.commit(commit_id),
  status         kg.commit_status NOT NULL DEFAULT 'open',
  -- Which engagement / tenant this commit belongs to. Every query in the
  -- platform is scoped by engagement_id -- this is the hard multi-tenant seam.
  engagement_id  uuid        NOT NULL,
  title          text        NOT NULL,
  rationale      text,
  -- The proposal that produced this commit. NULL only for the genesis commit.
  proposal_id    bigint,
  authored_by    text        NOT NULL,   -- agent runtime ARN or human principal
  sealed_by      text,                   -- human principal who approved the merge
  created_at     timestamptz NOT NULL DEFAULT now(),
  sealed_at      timestamptz,
  -- Content digest over the merged items, computed at seal time. Workflows
  -- pin to (commit_id, content_digest) so a tampered replay is detectable.
  content_digest text,
  CONSTRAINT commit_sealed_consistency CHECK (
    (status = 'open'     AND sealed_at IS NULL AND sealed_by IS NULL)
    OR (status <> 'open' AND sealed_at IS NOT NULL)
  )
);

CREATE INDEX commit_engagement_idx ON kg.commit (engagement_id, created_at DESC);
CREATE INDEX commit_parent_idx     ON kg.commit (parent_id);
CREATE UNIQUE INDEX commit_one_open_per_engagement
  ON kg.commit (engagement_id) WHERE status = 'open';

-- ---------------------------------------------------------------------
-- Sources and evidence -- provenance is not optional
-- ---------------------------------------------------------------------
CREATE TABLE kg.source (
  source_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id uuid NOT NULL,
  source_kind   text NOT NULL CHECK (source_kind IN (
                  'interview','sop_document','screen_recording','system_export',
                  'observation','sme_assertion','sor_telemetry','agent_inference')),
  uri           text,                 -- s3:// pointer to the raw artifact
  title         text NOT NULL,
  captured_at   timestamptz NOT NULL,
  captured_by   text,                 -- FDE or agent that captured it
  checksum      text,                 -- sha256 of the raw artifact
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX source_engagement_idx ON kg.source (engagement_id, captured_at DESC);
CREATE INDEX source_metadata_gin   ON kg.source USING gin (metadata jsonb_path_ops);

-- ---------------------------------------------------------------------
-- Nodes
-- ---------------------------------------------------------------------
-- node_key is the STABLE identity across versions. (engagement_id, node_key)
-- is what edges point at. node_id identifies one *version* of that node.
CREATE TABLE kg.node (
  node_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id uuid           NOT NULL,
  node_key      text           NOT NULL,   -- slug, stable across versions
  node_type     kg.node_type   NOT NULL,
  label         text           NOT NULL,
  summary       text,                      -- 1-3 sentences, what gets embedded
  attributes    jsonb          NOT NULL DEFAULT '{}'::jsonb,

  -- Valid time: when this fact was true of the business.
  valid_from    timestamptz    NOT NULL DEFAULT now(),
  valid_to      timestamptz,               -- NULL = currently valid

  -- Transaction time: when we recorded it.
  tx_from       timestamptz    NOT NULL DEFAULT now(),
  tx_to         timestamptz,               -- NULL = current belief

  commit_id     bigint         NOT NULL REFERENCES kg.commit(commit_id),
  superseded_by bigint         REFERENCES kg.node(node_id),

  confidence    real           NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  created_at    timestamptz    NOT NULL DEFAULT now(),

  CONSTRAINT node_valid_range CHECK (valid_to IS NULL OR valid_to > valid_from),
  CONSTRAINT node_tx_range    CHECK (tx_to    IS NULL OR tx_to    > tx_from),
  -- Tripwire against writing a person's name where a role belongs.
  CONSTRAINT node_role_is_not_a_person CHECK (
    node_type <> 'role' OR attributes ? 'is_role_title'
  )
);

-- Exactly one live version per (engagement, node_key).
CREATE UNIQUE INDEX node_current_key_uq
  ON kg.node (engagement_id, node_key)
  WHERE valid_to IS NULL AND tx_to IS NULL;

CREATE INDEX node_current_type_idx
  ON kg.node (engagement_id, node_type)
  WHERE valid_to IS NULL AND tx_to IS NULL;

CREATE INDEX node_history_idx  ON kg.node (engagement_id, node_key, valid_from DESC);
CREATE INDEX node_commit_idx   ON kg.node (commit_id);
CREATE INDEX node_attrs_gin    ON kg.node USING gin (attributes jsonb_path_ops);
CREATE INDEX node_label_trgm   ON kg.node USING gin (label gin_trgm_ops);

-- ---------------------------------------------------------------------
-- Edges
-- ---------------------------------------------------------------------
-- Edges reference node_key (stable identity), not node_id (version), so a
-- node can be re-versioned without rewriting every edge that points at it.
CREATE TABLE kg.edge (
  edge_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id uuid          NOT NULL,
  edge_key      text          NOT NULL,  -- deterministic: src|type|dst|qualifier
  src_key       text          NOT NULL,
  dst_key       text          NOT NULL,
  edge_type     kg.edge_type  NOT NULL,
  label         text,
  -- Branch conditions, SLAs, cardinality, observed frequency, etc.
  attributes    jsonb         NOT NULL DEFAULT '{}'::jsonb,
  weight        real          NOT NULL DEFAULT 1.0,

  valid_from    timestamptz   NOT NULL DEFAULT now(),
  valid_to      timestamptz,
  tx_from       timestamptz   NOT NULL DEFAULT now(),
  tx_to         timestamptz,

  commit_id     bigint        NOT NULL REFERENCES kg.commit(commit_id),
  confidence    real          NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  -- Set by the HITL merge path. An edge that no human ever confirmed is
  -- still usable for retrieval but is excluded from `wf` authoring by default.
  human_confirmed boolean     NOT NULL DEFAULT false,
  created_at    timestamptz   NOT NULL DEFAULT now(),

  CONSTRAINT edge_valid_range CHECK (valid_to IS NULL OR valid_to > valid_from),
  CONSTRAINT edge_tx_range    CHECK (tx_to    IS NULL OR tx_to    > tx_from),
  CONSTRAINT edge_no_self_loop CHECK (src_key <> dst_key OR edge_type = 'supersedes')
);

CREATE UNIQUE INDEX edge_current_key_uq
  ON kg.edge (engagement_id, edge_key)
  WHERE valid_to IS NULL AND tx_to IS NULL;

-- The two hot traversal indexes. Forward and reverse, both filtered to live
-- rows so the index stays small as history accumulates.
CREATE INDEX edge_fwd_idx
  ON kg.edge (engagement_id, src_key, edge_type, dst_key)
  WHERE valid_to IS NULL AND tx_to IS NULL;

CREATE INDEX edge_rev_idx
  ON kg.edge (engagement_id, dst_key, edge_type, src_key)
  WHERE valid_to IS NULL AND tx_to IS NULL;

-- Dependency traversal is the most-run query; give it a dedicated partial index.
CREATE INDEX edge_depends_idx
  ON kg.edge (engagement_id, src_key, dst_key)
  WHERE edge_type = 'depends_on' AND valid_to IS NULL AND tx_to IS NULL;

CREATE INDEX edge_history_idx ON kg.edge (engagement_id, edge_key, valid_from DESC);
CREATE INDEX edge_commit_idx  ON kg.edge (commit_id);
CREATE INDEX edge_attrs_gin   ON kg.edge USING gin (attributes jsonb_path_ops);

-- ---------------------------------------------------------------------
-- Evidence: many sources may corroborate one edge, each with its own
-- confidence. Modelled as a join table rather than a column so corroboration
-- can be aggregated instead of overwritten.
-- ---------------------------------------------------------------------
CREATE TABLE kg.evidence (
  evidence_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id  uuid   NOT NULL,
  subject_kind   text   NOT NULL CHECK (subject_kind IN ('node','edge')),
  subject_key    text   NOT NULL,          -- node_key or edge_key
  source_id      bigint NOT NULL REFERENCES kg.source(source_id),
  excerpt        text,                     -- the literal span that supports it
  locator        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- page, timestamp, line
  extraction_method text NOT NULL CHECK (extraction_method IN
                     ('llm_extraction','human_annotation','rule_based','sor_derived')),
  confidence     real   NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  extracted_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (engagement_id, subject_kind, subject_key, source_id, extraction_method)
);
CREATE INDEX evidence_subject_idx ON kg.evidence (engagement_id, subject_kind, subject_key);
CREATE INDEX evidence_source_idx  ON kg.evidence (source_id);

-- ---------------------------------------------------------------------
-- Current-state views. Every read path in the platform goes through these
-- unless it is explicitly doing a point-in-time query.
-- ---------------------------------------------------------------------
CREATE VIEW kg.node_current AS
  SELECT * FROM kg.node WHERE valid_to IS NULL AND tx_to IS NULL;

CREATE VIEW kg.edge_current AS
  SELECT * FROM kg.edge WHERE valid_to IS NULL AND tx_to IS NULL;

-- Point-in-time: what the graph asserted at valid time `at_valid`, as
-- believed at transaction time `at_tx`. This is what makes a workflow
-- reproducible six months after it was authored.
CREATE FUNCTION kg.node_as_of(p_engagement uuid, at_valid timestamptz, at_tx timestamptz DEFAULT now())
RETURNS SETOF kg.node
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT * FROM kg.node
   WHERE engagement_id = p_engagement
     AND valid_from <= at_valid AND (valid_to IS NULL OR valid_to > at_valid)
     AND tx_from    <= at_tx    AND (tx_to    IS NULL OR tx_to    > at_tx);
$$;

CREATE FUNCTION kg.edge_as_of(p_engagement uuid, at_valid timestamptz, at_tx timestamptz DEFAULT now())
RETURNS SETOF kg.edge
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT * FROM kg.edge
   WHERE engagement_id = p_engagement
     AND valid_from <= at_valid AND (valid_to IS NULL OR valid_to > at_valid)
     AND tx_from    <= at_tx    AND (tx_to    IS NULL OR tx_to    > at_tx);
$$;

-- Aggregated evidence strength for an edge -- used by retrieval ranking and
-- by the promotion gate (an edge below threshold cannot auto-merge).
CREATE FUNCTION kg.evidence_strength(p_engagement uuid, p_kind text, p_key text)
RETURNS real
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  -- Noisy-OR over independent sources: 1 - PROD(1 - c_i).
  -- Two 0.7 sources beat one 0.9 source, which is the behaviour we want
  -- from corroborating interviews.
  SELECT COALESCE(1.0 - exp(sum(ln(greatest(1.0 - confidence, 1e-6)))), 0.0)::real
    FROM kg.evidence
   WHERE engagement_id = p_engagement
     AND subject_kind  = p_kind
     AND subject_key   = p_key;
$$;
