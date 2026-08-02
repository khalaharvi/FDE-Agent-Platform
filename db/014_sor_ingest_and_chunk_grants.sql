-- =====================================================================
-- 014_sor_ingest_and_chunk_grants.sql
-- Observation idempotency for the SoR adapters, plus the grants the
-- evidence-chunk ingestion tools need.
--
-- Grants-only migrations (011, 012) exist in this repo because 010's
-- `ALL TABLES IN SCHEMA` grants are point-in-time and its role design
-- predated half the tool surface. This one follows the same pattern: it
-- names each grant, says which code path failed without it, and adds no
-- capability beyond that path.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Observation idempotency.
--
-- sor.observation has no unique key today, and a watermark alone does not
-- give one. Three ordinary situations produce duplicate rows:
--   * a crash between the batch insert and the cursor advance replays the
--     whole batch on the next run;
--   * SQS delivery is at-least-once by contract, so an event_stream
--     adapter WILL see the same message twice;
--   * a backfill and a live poll routinely overlap at the seam.
-- None of the detectors in 007 deduplicate. detect_control_bypass computes
-- a rate over counted rows, so a replayed batch does not merely add noise
-- -- it moves the number the severity threshold is read from.
--
-- dedup_key is sha256(case_ref|raw_activity|occurred_at|actor_hash),
-- computed by the adapter. adapter_id lives in the index rather than the
-- hash so the same record arriving through two adapters stays two
-- observations (it is two independent measurements). Hand-written inserts
-- -- the smoke test's, anyone's psql session -- leave it NULL and are
-- unaffected, which is what the partial index is for.
-- ---------------------------------------------------------------------
ALTER TABLE sor.observation ADD COLUMN dedup_key text;

COMMENT ON COLUMN sor.observation.dedup_key IS
  'sha256(case_ref|raw_activity|occurred_at|actor_hash), set by fde-sor '
  'adapters so at-least-once delivery cannot double-count. NULL for '
  'hand-written inserts, which the partial unique index below ignores.';

CREATE UNIQUE INDEX observation_dedup_uq
  ON sor.observation (adapter_id, dedup_key) WHERE dedup_key IS NOT NULL;

-- ---------------------------------------------------------------------
-- Evidence-chunk ingestion (kg_ingest_chunks in the MCP tool surface).
--
-- 010:38 gave fde_agent INSERT on kg.source -- "may register evidence it
-- captured" -- but nothing on kg.chunk or kg.embed_queue, so an agent
-- could register a source it could never attach text to, and the chunk
-- granularity of kg.hybrid_search stayed permanently empty.
--
-- INSERT only, deliberately. Chunks are the verbatim evidence a reviewer
-- reads to check a proposal; an agent that could UPDATE kg.chunk could
-- rewrite the evidence for a claim after a human agreed to it. Re-chunking
-- means registering a new kg.source, which leaves the old text in place.
-- ---------------------------------------------------------------------
GRANT INSERT ON kg.chunk TO fde_agent;

-- The same tool enqueues each new chunk for embedding. Without this the
-- chunks land but never get an embedding, and a chunk with no embedding is
-- invisible to kg.ann_chunks (008:251 filters embedding IS NOT NULL) --
-- ingestion that silently produces unretrievable evidence.
-- ON CONFLICT (subject_kind, subject_id) DO NOTHING needs no UPDATE: a
-- chunk already queued is already queued.
GRANT INSERT ON kg.embed_queue TO fde_agent;

-- ---------------------------------------------------------------------
-- Drift-scan escalation.
--
-- The scheduled drift scan runs as fde_ingest (010:60 grants it EXECUTE on
-- sor.run_all_detectors) and then has to read back which signals the scan
-- just raised, to publish the critical control_bypass ones to SNS.
-- 010:55-61 gave fde_ingest no SELECT on sor.drift_signal at all, so the
-- detector could write signals the scanner could not see. Read-only: the
-- triage transitions belong to fde_prodops (010:68) and fde_agent (011:33).
-- ---------------------------------------------------------------------
GRANT SELECT ON sor.drift_signal TO fde_ingest;

-- ---------------------------------------------------------------------
-- Deliberately NOT granted, listed so the omissions read as decisions:
--   * UPDATE / DELETE on kg.chunk to any runtime role -- chunk immutability
--     is the point of the INSERT-only grant above.
--   * INSERT on sor.adapter to fde_ingest. The mapping jsonb IS the
--     contract that makes drift measurable (007:34-36) and docs/08 §2.5
--     makes authoring it a human act at onboarding. A runtime role that
--     could register its own measurement instrument could also define away
--     the drift it is measured by. Registration runs on the operator DSN.
--   * Any change to the six CI privilege-denial invariants. Nothing here
--     touches kg.node, hitl.merge_proposal, wf.workflow or
--     trn.trace_session.outcome.
-- ---------------------------------------------------------------------
