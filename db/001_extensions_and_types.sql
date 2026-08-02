-- =====================================================================
-- 001_extensions_and_types.sql
-- FDE Agent Platform — extensions, enums, and shared domains
--
-- Target: PostgreSQL 16/17 with pgvector >= 0.8.0
--   Aurora PostgreSQL 16.8+/15.12+/17.x  -> pgvector 0.8.0+
--   RDS PostgreSQL 17.1+/16.5+/15.9+     -> pgvector 0.8.0+
--
-- pgvector 0.8.0 is the MINIMUM. It introduced hnsw.iterative_scan, which
-- this schema depends on for correctness (not just speed) on filtered ANN
-- queries. On < 0.8.0 a filtered `ORDER BY embedding <=> q LIMIT k` silently
-- returns FEWER than k rows -- retrieval looks like it "found nothing" when
-- matching rows exist. Do not deploy this on an older pgvector.
--
-- Apache AGE is deliberately NOT used: it is not available on RDS or Aurora
-- (C extension outside AWS's allowlist). All traversal is plain SQL.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;      -- HNSW ANN
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- lexical fallback / fuzzy entity match
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_uuid, digest()
CREATE EXTENSION IF NOT EXISTS btree_gin;   -- composite GIN on scalar + jsonb

-- Guard: fail loudly on an unsupported pgvector.
DO $$
DECLARE v text;
BEGIN
  SELECT extversion INTO v FROM pg_extension WHERE extname = 'vector';
  IF string_to_array(v, '.')::int[] < ARRAY[0,8,0] THEN
    RAISE EXCEPTION
      'pgvector % is too old. >= 0.8.0 required (hnsw.iterative_scan). '
      'Filtered ANN queries return incomplete results on older versions.', v;
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS kg;      -- knowledge graph (system of truth for "how work is done")
CREATE SCHEMA IF NOT EXISTS hitl;    -- human gates, proposals, decisions
CREATE SCHEMA IF NOT EXISTS wf;      -- workflows + product-ops runs
CREATE SCHEMA IF NOT EXISTS sor;     -- system-of-record adapters + drift
CREATE SCHEMA IF NOT EXISTS trn;     -- training telemetry, traces, graders

-- ---------------------------------------------------------------------
-- Embedding contract
-- ---------------------------------------------------------------------
-- 1024 dims: amazon.titan-embed-text-v2:0 (dimensions=1024, normalize=true)
--            or cohere.embed-v4 with output_dimension=1024.
-- Under pgvector's 2000-dim HNSW ceiling for `vector`, so no halfvec needed
-- for indexability -- halfvec is used anyway to halve index memory (see 003).
-- Changing this constant is a migration, not a config change.
CREATE DOMAIN kg.embedding AS vector(1024);

-- ---------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------

-- The FDE ontology. Node types are closed: the Engagement Agent may not
-- invent new ones at runtime. Adding a type is a schema migration reviewed
-- by a human -- this is the first of the deterministic constraints.
CREATE TYPE kg.node_type AS ENUM (
  'org_unit',       -- team, department, function
  'role',           -- job role (never a named person -- see PII note below)
  'system',         -- a system of record or tool (Salesforce, Jira, MuleSoft...)
  'system_object',  -- an entity inside a system (Opportunity, Ticket, Quote)
  'capability',     -- business capability
  'process',        -- end-to-end business process
  'activity',       -- a single step performed within a process
  'artifact',       -- document/data object produced or consumed
  'decision',       -- explicit decision point with branch conditions
  'control',        -- policy, compliance, or approval control
  'metric',         -- KPI or operational measure
  'pain_point',     -- observed bottleneck / friction
  'opportunity',    -- scored AI/automation opportunity
  'tool_binding',   -- a concrete callable tool an agent could use
  'evidence_doc'    -- interview note, SOP, screen recording, ticket export
);

-- Edge types are likewise closed. `depends_on` is the backbone of the
-- dependency graph the platform is named for.
CREATE TYPE kg.edge_type AS ENUM (
  'belongs_to',     -- activity -> process ; role -> org_unit
  'performs',       -- role -> activity
  'precedes',       -- activity -> activity (control flow)
  'hands_off_to',   -- activity -> activity across a role or system boundary
  'produces',       -- activity -> artifact
  'consumes',       -- activity -> artifact
  'recorded_in',    -- activity -> system_object (where the fact lands)
  'depends_on',     -- activity -> system | artifact | activity (hard dependency)
  'gated_by',       -- activity -> decision | control
  'measured_by',    -- process | activity -> metric
  'blocks',         -- pain_point -> activity
  'addresses',      -- opportunity -> pain_point
  'automatable_by', -- activity -> tool_binding
  'evidenced_by',   -- any node -> evidence_doc
  'supersedes'      -- node -> node (lineage across a rewrite)
);

CREATE TYPE kg.commit_status AS ENUM ('open', 'sealed', 'reverted');

CREATE TYPE hitl.proposal_status AS ENUM (
  'draft',        -- agent still assembling
  'submitted',    -- awaiting human gate
  'in_review',    -- a reviewer has claimed it
  'changes_requested',
  'approved',     -- passed every required gate
  'rejected',
  'merged',       -- promoted into kg.node/kg.edge under a commit
  'expired'       -- SLA blew through; auto-closed
);

CREATE TYPE hitl.decision AS ENUM ('approve', 'reject', 'request_changes', 'abstain');

CREATE TYPE hitl.gate_kind AS ENUM (
  'ontology',     -- is this the right node/edge type? does it duplicate an existing node?
  'factual',      -- does this match how work is actually done? (SME sign-off)
  'control',      -- does this touch a compliance control? (risk/compliance sign-off)
  'automation'    -- may an agent execute this step unattended? (process owner sign-off)
);

CREATE TYPE wf.workflow_status AS ENUM ('draft', 'review', 'published', 'deprecated');
CREATE TYPE wf.run_status AS ENUM ('pending', 'running', 'awaiting_human', 'succeeded', 'failed', 'cancelled');
CREATE TYPE wf.step_kind AS ENUM ('agent', 'tool', 'human', 'decision', 'sor_write', 'notify');

CREATE TYPE sor.drift_kind AS ENUM (
  'missing_in_sor',    -- graph asserts a step that the SoR never records
  'missing_in_graph',  -- SoR shows an activity/transition the graph does not model
  'sequence_drift',    -- observed ordering diverges from `precedes` edges
  'actor_drift',       -- a different role performs it than `performs` says
  'latency_drift',     -- step duration has moved outside the modelled band
  'volume_drift',      -- throughput moved outside the modelled band
  'control_bypass',    -- a `gated_by` control was skipped
  'stale_pin'          -- workflow pinned to a commit that is now N commits behind
);

CREATE TYPE sor.drift_severity AS ENUM ('info', 'low', 'medium', 'high', 'critical');
CREATE TYPE sor.drift_state AS ENUM ('open', 'triaged', 'proposal_raised', 'accepted', 'dismissed', 'resolved');

CREATE TYPE trn.trace_outcome AS ENUM ('pending', 'accepted', 'rejected', 'corrected');
CREATE TYPE trn.split AS ENUM ('train', 'validation', 'test', 'holdout');

-- ---------------------------------------------------------------------
-- PII posture
-- ---------------------------------------------------------------------
-- The graph models ROLES, not people. `kg.node` of type 'role' holds a role
-- title and an org_unit edge. Individual identities live only in `hitl.reviewer`
-- (needed for accountable sign-off) and in SoR observations as an opaque
-- `actor_hash`. Never write a person's name into kg.node.label. The
-- constraint below is a tripwire, not a guarantee -- pair it with review.
-- ---------------------------------------------------------------------
