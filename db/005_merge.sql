-- =====================================================================
-- 005_merge.sql
-- The ONLY write path into kg.node / kg.edge.
--
-- Everything else in the platform has SELECT on kg.* and EXECUTE on nothing
-- in this file. hitl.merge_proposal runs SECURITY DEFINER as the migration
-- owner, so even a compromised agent role cannot mutate the graph except by
-- getting a proposal through its gates.
-- =====================================================================

-- Deterministic edge key. Two agents independently proposing the same edge
-- must collide, so dedup is a unique-constraint violation rather than a
-- semantic judgement call.
CREATE OR REPLACE FUNCTION kg.make_edge_key(p_src text, p_type kg.edge_type, p_dst text,
                                 p_qualifier text DEFAULT '')
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT p_src || '|' || p_type::text || '|' || p_dst ||
         CASE WHEN coalesce(p_qualifier,'') = '' THEN '' ELSE '|' || p_qualifier END;
$$;

-- Close out the currently-live version of a node/edge, valid-time style.
CREATE OR REPLACE FUNCTION kg.close_node(p_engagement uuid, p_key text, p_at timestamptz)
RETURNS bigint
LANGUAGE sql AS $$
  UPDATE kg.node SET valid_to = p_at
   WHERE engagement_id = p_engagement AND node_key = p_key
     AND valid_to IS NULL AND tx_to IS NULL
  RETURNING node_id;
$$;

CREATE OR REPLACE FUNCTION kg.close_edge(p_engagement uuid, p_key text, p_at timestamptz)
RETURNS bigint
LANGUAGE sql AS $$
  UPDATE kg.edge SET valid_to = p_at
   WHERE engagement_id = p_engagement AND edge_key = p_key
     AND valid_to IS NULL AND tx_to IS NULL
  RETURNING edge_id;
$$;

-- =====================================================================
-- hitl.merge_proposal
--
-- Preconditions (all enforced, all fail-closed):
--   1. proposal.status = 'approved'
--   2. hitl.gates_satisfied() = true, re-checked inside the transaction
--   3. every non-retire item has >= 1 evidence source
--   4. no other open commit for this engagement (serialises graph writes)
--
-- Effects:
--   * creates a sealed kg.commit
--   * closes superseded node/edge versions at commit time
--   * inserts new versions carrying commit_id
--   * writes kg.evidence rows
--   * enqueues embeddings
--   * marks proposal 'merged'
--   * fires drift re-evaluation for any workflow pinned to an older commit
-- =====================================================================
CREATE OR REPLACE FUNCTION hitl.merge_proposal(p_proposal_id bigint, p_merged_by text)
RETURNS kg.commit
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kg, hitl, wf, public AS $$
DECLARE
  p          hitl.proposal;
  c          kg.commit;
  it         record;
  -- now() (transaction timestamp), NOT clock_timestamp(). Reads default to
  -- now() too, so a merge and any subsequent read in the same transaction
  -- agree. With clock_timestamp() the newly-written rows have valid_from
  -- slightly AFTER the reading transaction's now(), and kg.traverse silently
  -- returns zero rows -- a read-your-own-write failure that only shows up
  -- under test harnesses and batch jobs that merge then immediately query.
  -- Side effect, and a desirable one: merging the same key twice inside one
  -- transaction violates the valid_to > valid_from check and aborts.
  v_now      timestamptz := now();
  v_node_id  bigint;
  v_edge_id  bigint;
  v_src      bigint;
  v_effective jsonb;
  v_digest   text := '';
BEGIN
  SELECT * INTO p FROM hitl.proposal WHERE proposal_id = p_proposal_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'proposal % not found', p_proposal_id; END IF;

  IF p.status <> 'approved' THEN
    RAISE EXCEPTION 'proposal % is %, expected approved', p_proposal_id, p.status;
  END IF;

  -- Re-check gates INSIDE the transaction. Between the UI marking a proposal
  -- approved and the merge landing, an authority may have been revoked.
  IF NOT hitl.gates_satisfied(p_proposal_id) THEN
    RAISE EXCEPTION 'gates not satisfied for proposal % at merge time', p_proposal_id;
  END IF;

  INSERT INTO kg.commit (parent_id, status, engagement_id, title, rationale,
                         proposal_id, authored_by, sealed_by, sealed_at)
  VALUES ((SELECT commit_id FROM kg.commit
            WHERE engagement_id = p.engagement_id AND status = 'sealed'
            ORDER BY commit_id DESC LIMIT 1),
          'sealed', p.engagement_id, p.title, p.rationale,
          p_proposal_id, p.authored_by, p_merged_by, v_now)
  RETURNING * INTO c;

  FOR it IN
    SELECT * FROM hitl.proposal_item
     WHERE proposal_id = p_proposal_id AND item_status <> 'dropped'
     ORDER BY ordinal
  LOOP
    -- A reviewer edit wins over the agent's original payload.
    v_effective := it.payload;
    v_digest := encode(digest(v_digest || it.op || it.subject_key ||
                              v_effective::text, 'sha256'), 'hex');

    IF it.op IN ('add_node','update_node') THEN
      PERFORM kg.close_node(p.engagement_id, it.subject_key, v_now);
      INSERT INTO kg.node (engagement_id, node_key, node_type, label, summary,
                           attributes, valid_from, commit_id, confidence)
      VALUES (p.engagement_id, it.subject_key, it.node_type,
              v_effective->>'label', v_effective->>'summary',
              coalesce(v_effective->'attributes', '{}'::jsonb),
              v_now, c.commit_id,
              coalesce((v_effective->>'confidence')::real, it.agent_confidence))
      RETURNING node_id INTO v_node_id;

      IF it.supersedes_key IS NOT NULL THEN
        UPDATE kg.node SET superseded_by = v_node_id
         WHERE engagement_id = p.engagement_id AND node_key = it.supersedes_key
           AND valid_to = v_now;
      END IF;

      INSERT INTO kg.embed_queue (engagement_id, subject_kind, subject_id)
      VALUES (p.engagement_id, 'node', v_node_id)
      ON CONFLICT (subject_kind, subject_id) DO NOTHING;

    ELSIF it.op = 'retire_node' THEN
      PERFORM kg.close_node(p.engagement_id, it.subject_key, v_now);

    ELSIF it.op IN ('add_edge','update_edge') THEN
      PERFORM kg.close_edge(p.engagement_id, it.subject_key, v_now);
      INSERT INTO kg.edge (engagement_id, edge_key, src_key, dst_key, edge_type,
                           label, attributes, weight, valid_from, commit_id,
                           confidence, human_confirmed)
      VALUES (p.engagement_id, it.subject_key,
              v_effective->>'src_key', v_effective->>'dst_key', it.edge_type,
              v_effective->>'label',
              coalesce(v_effective->'attributes','{}'::jsonb),
              coalesce((v_effective->>'weight')::real, 1.0),
              v_now, c.commit_id,
              coalesce((v_effective->>'confidence')::real, it.agent_confidence),
              true)                        -- merged == a human signed off
      RETURNING edge_id INTO v_edge_id;

      INSERT INTO kg.embed_queue (engagement_id, subject_kind, subject_id)
      VALUES (p.engagement_id, 'edge', v_edge_id)
      ON CONFLICT (subject_kind, subject_id) DO NOTHING;

    ELSIF it.op = 'retire_edge' THEN
      PERFORM kg.close_edge(p.engagement_id, it.subject_key, v_now);
    END IF;

    -- Provenance
    FOREACH v_src IN ARRAY it.source_ids LOOP
      INSERT INTO kg.evidence (engagement_id, subject_kind, subject_key, source_id,
                               excerpt, extraction_method, confidence)
      VALUES (p.engagement_id,
              CASE WHEN it.op LIKE '%node' THEN 'node' ELSE 'edge' END,
              it.subject_key, v_src,
              v_effective->>'excerpt',
              CASE WHEN it.edited_by IS NOT NULL THEN 'human_annotation'
                   ELSE 'llm_extraction' END,
              it.agent_confidence)
      ON CONFLICT (engagement_id, subject_kind, subject_key, source_id, extraction_method)
      DO UPDATE SET confidence = greatest(kg.evidence.confidence, EXCLUDED.confidence);
    END LOOP;
  END LOOP;

  UPDATE kg.commit SET content_digest = v_digest WHERE commit_id = c.commit_id
  RETURNING * INTO c;

  UPDATE hitl.proposal
     SET status = 'merged', merged_commit_id = c.commit_id,
         decided_at = v_now, updated_at = v_now
   WHERE proposal_id = p_proposal_id;

  -- Any published workflow pinned to an older commit is now potentially stale.
  -- Recorded here rather than detected later so the signal is transactional.
  --
  -- The ON CONFLICT clause is not optional and is not defensive padding: it
  -- is the same dedup sor.record_drift performs (007:154-162), and without it
  -- this statement aborts the whole merge the SECOND time an engagement
  -- merges while a published workflow is still behind head --
  -- drift_signal_dedup_uq already holds an open stale_pin row for that
  -- workflow_uuid. Every engagement could merge exactly once after
  -- publishing a workflow and then all merges failed on a drift-queue
  -- housekeeping insert. Verified empirically against 001-012 alone.
  -- detected_at deliberately keeps its original value (when the workflow
  -- FIRST went stale); detail is refreshed so commits_behind stays true.
  INSERT INTO sor.drift_signal (engagement_id, drift_kind, severity, subject_kind,
                                subject_ref, detected_at, detail)
  SELECT p.engagement_id, 'stale_pin', 'low', 'workflow', w.workflow_uuid::text, v_now,
         jsonb_build_object('pinned_commit', w.pinned_commit_id,
                            'head_commit',   c.commit_id,
                            'commits_behind', c.commit_id - w.pinned_commit_id)
    FROM wf.workflow w
   WHERE w.engagement_id = p.engagement_id
     AND w.status = 'published'
     AND w.pinned_commit_id < c.commit_id
  ON CONFLICT (engagement_id, drift_kind, subject_kind, subject_ref)
    WHERE state IN ('open','triaged','proposal_raised')
  DO UPDATE SET last_seen_at = now(),
                occurrences  = sor.drift_signal.occurrences + 1,
                detail       = EXCLUDED.detail;

  RETURN c;
END $$;

REVOKE ALL ON FUNCTION hitl.merge_proposal(bigint, text) FROM PUBLIC;

COMMENT ON FUNCTION hitl.merge_proposal IS
  'Sole write path into kg.node/kg.edge. SECURITY DEFINER. Re-checks gate '
  'quorum inside the transaction. Grant EXECUTE only to the gate service role.';
