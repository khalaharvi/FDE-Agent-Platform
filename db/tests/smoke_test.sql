-- =====================================================================
-- smoke_test.sql
-- End-to-end proof that the deterministic HITL spine actually holds.
--
-- Run:  psql -d fde -v ON_ERROR_STOP=1 -f db/tests/smoke_test.sql
-- Every assertion RAISEs on failure. Silence + "ALL SMOKE TESTS PASSED"
-- at the end is the pass condition.
--
-- Scenario: a Quote-to-Cash discount approval process. The Engagement Agent
-- proposes six graph facts from two interviews. Gates fire. A merge is
-- attempted early and must fail. Reviewers sign off. Merge succeeds.
-- Traversal, dependency closure, and hybrid search return the right answers.
-- Then SoR observations are injected showing a control being bypassed, and
-- the drift detector must catch it.
--
-- TEST 17-22 continue the same engagement through the gate service's own
-- transitions (013): a reviewer decides, changes their mind, edits an item,
-- rejects a second proposal, publishes a workflow, and runs it -- including
-- a human step, a decision branch, every on_failure policy, and both
-- scheduled sweeps. TEST 23-24 cover the ingest grants and observation
-- idempotency (014). TEST 25 covers point-in-time closure (015).
-- =====================================================================
\set ON_ERROR_STOP on
\set QUIET on
\pset pager off
SET client_min_messages = notice;

BEGIN;

CREATE OR REPLACE FUNCTION pg_temp.assert(cond boolean, msg text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT cond THEN RAISE EXCEPTION 'ASSERTION FAILED: %', msg; END IF;
END $$;

DO $$
DECLARE
  eng        uuid := '11111111-1111-1111-1111-111111111111';
  genesis    bigint;
  src_a      bigint; src_b bigint;
  prop       bigint;
  rev_sme    bigint; rev_comp bigint; rev_owner bigint;
  gate       record;
  c          kg.commit;
  n_gates    int;
  n          int;
  merged_ok  boolean;
  rows_out   int;
  qvec       kg.embedding;
BEGIN
  ------------------------------------------------------------------
  RAISE NOTICE '--- setup: genesis commit, sources, reviewers';
  ------------------------------------------------------------------
  INSERT INTO kg.commit (status, engagement_id, title, authored_by, sealed_by, sealed_at)
  VALUES ('sealed', eng, 'genesis', 'bootstrap', 'bootstrap', now())
  RETURNING commit_id INTO genesis;

  INSERT INTO kg.source (engagement_id, source_kind, title, captured_at, captured_by)
  VALUES (eng, 'interview', 'RevOps lead interview 2026-07-14', now() - interval '3 days', 'fde:kh')
  RETURNING source_id INTO src_a;

  INSERT INTO kg.source (engagement_id, source_kind, title, captured_at, captured_by)
  VALUES (eng, 'sop_document', 'Discount Approval SOP v4', now() - interval '10 days', 'fde:kh')
  RETURNING source_id INTO src_b;

  INSERT INTO hitl.reviewer (principal, display_name) VALUES
    ('sme@example.com','RevOps SME')      RETURNING reviewer_id INTO rev_sme;
  INSERT INTO hitl.reviewer (principal, display_name) VALUES
    ('compliance@example.com','Compliance') RETURNING reviewer_id INTO rev_comp;
  INSERT INTO hitl.reviewer (principal, display_name) VALUES
    ('owner@example.com','Process Owner')  RETURNING reviewer_id INTO rev_owner;

  INSERT INTO hitl.reviewer_authority (reviewer_id, engagement_id, gate_kind, granted_by) VALUES
    (rev_sme,   eng, 'ontology',   'bootstrap'),
    (rev_sme,   eng, 'factual',    'bootstrap'),
    (rev_comp,  eng, 'ontology',   'bootstrap'),
    (rev_comp,  eng, 'control',    'bootstrap'),
    (rev_owner, eng, 'factual',    'bootstrap'),
    (rev_owner, eng, 'automation', 'bootstrap');

  ------------------------------------------------------------------
  RAISE NOTICE '--- agent proposes graph facts';
  ------------------------------------------------------------------
  INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                             agent_name, base_commit_id, model_id)
  VALUES (eng, 'Q2C discount approval — initial mapping',
          'Derived from RevOps interview + SOP v4. Two independent sources agree '
          'on the >20% escalation threshold.',
          'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-engagement',
          'engagement', genesis, 'claude-sonnet-4')
  RETURNING proposal_id INTO prop;

  INSERT INTO hitl.proposal_item
    (proposal_id, ordinal, op, node_type, subject_key, payload, source_ids, agent_confidence)
  VALUES
   (prop, 1, 'add_node', 'process', 'proc.quote_to_cash',
    '{"label":"Quote to Cash","summary":"End-to-end from quote creation to booked revenue.","attributes":{}}',
    ARRAY[src_a, src_b], 0.92),
   (prop, 2, 'add_node', 'activity', 'act.create_quote',
    '{"label":"Create Quote","summary":"Sales rep builds a quote in CPQ.","attributes":{}}',
    ARRAY[src_a], 0.95),
   (prop, 3, 'add_node', 'activity', 'act.discount_review',
    '{"label":"Discount Review","summary":"Deal desk reviews quotes discounted beyond policy.","attributes":{}}',
    ARRAY[src_a, src_b], 0.90),
   (prop, 4, 'add_node', 'control', 'ctl.discount_threshold_20',
    '{"label":"20% Discount Threshold","summary":"Quotes above 20% discount require deal desk approval before send.","attributes":{"data_classification":"internal"}}',
    ARRAY[src_b], 0.88),
   (prop, 5, 'add_node', 'system', 'sys.cpq',
    '{"label":"CPQ","summary":"Configure-price-quote system of record for quotes.","attributes":{}}',
    ARRAY[src_a], 0.97),
   (prop, 6, 'add_node', 'role', 'role.deal_desk',
    '{"label":"Deal Desk Analyst","summary":"Reviews and approves non-standard pricing.","attributes":{"is_role_title":true}}',
    ARRAY[src_a], 0.94);

  INSERT INTO hitl.proposal_item
    (proposal_id, ordinal, op, edge_type, subject_key, payload, source_ids, agent_confidence)
  VALUES
   (prop, 7, 'add_edge', 'belongs_to',
    kg.make_edge_key('act.create_quote','belongs_to','proc.quote_to_cash'),
    '{"src_key":"act.create_quote","dst_key":"proc.quote_to_cash"}', ARRAY[src_a], 0.95),
   (prop, 8, 'add_edge', 'belongs_to',
    kg.make_edge_key('act.discount_review','belongs_to','proc.quote_to_cash'),
    '{"src_key":"act.discount_review","dst_key":"proc.quote_to_cash"}', ARRAY[src_a], 0.95),
   (prop, 9, 'add_edge', 'precedes',
    kg.make_edge_key('act.create_quote','precedes','act.discount_review'),
    '{"src_key":"act.create_quote","dst_key":"act.discount_review","attributes":{"sla_seconds":14400}}',
    ARRAY[src_a, src_b], 0.91),
   (prop, 10, 'add_edge', 'gated_by',
    kg.make_edge_key('act.create_quote','gated_by','ctl.discount_threshold_20'),
    '{"src_key":"act.create_quote","dst_key":"ctl.discount_threshold_20"}', ARRAY[src_b], 0.89),
   (prop, 11, 'add_edge', 'depends_on',
    kg.make_edge_key('act.discount_review','depends_on','sys.cpq'),
    '{"src_key":"act.discount_review","dst_key":"sys.cpq"}', ARRAY[src_a], 0.93),
   (prop, 12, 'add_edge', 'performs',
    kg.make_edge_key('role.deal_desk','performs','act.discount_review'),
    '{"src_key":"role.deal_desk","dst_key":"act.discount_review"}', ARRAY[src_a], 0.96);

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 1: submit computes a deterministic gate set';
  ------------------------------------------------------------------
  PERFORM hitl.submit_proposal(prop);
  SELECT count(*) INTO n_gates FROM hitl.proposal_gate WHERE proposal_id = prop;
  PERFORM pg_temp.assert(n_gates >= 3,
    format('expected >=3 gates (ontology + factual + control), got %s', n_gates));

  PERFORM pg_temp.assert(
    EXISTS (SELECT 1 FROM hitl.proposal_gate WHERE proposal_id = prop AND gate_kind = 'control'),
    'control gate must fire: proposal contains a control node and a gated_by edge');
  PERFORM pg_temp.assert(
    EXISTS (SELECT 1 FROM hitl.proposal_gate WHERE proposal_id = prop
              AND gate_kind = 'factual' AND quorum = 2),
    'process node must require a 2-reviewer factual quorum');

  -- Determinism: recomputing must give the identical gate set.
  PERFORM pg_temp.assert(
    (SELECT count(*) FROM hitl.compute_required_gates(prop)) = n_gates,
    'compute_required_gates is not deterministic across calls');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 2: merge is REFUSED before gates clear';
  ------------------------------------------------------------------
  PERFORM pg_temp.assert(NOT hitl.gates_satisfied(prop),
    'gates_satisfied must be false with zero decisions');

  UPDATE hitl.proposal SET status = 'approved' WHERE proposal_id = prop;
  merged_ok := true;
  BEGIN
    PERFORM hitl.merge_proposal(prop, 'attacker@example.com');
  EXCEPTION WHEN others THEN
    merged_ok := false;
  END;
  PERFORM pg_temp.assert(NOT merged_ok,
    'merge_proposal MUST refuse when gates are unsatisfied even if status=approved');
  PERFORM pg_temp.assert((SELECT count(*) FROM kg.node WHERE engagement_id = eng) = 0,
    'no graph rows may exist after a refused merge');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 3: partial quorum still blocks';
  ------------------------------------------------------------------
  FOR gate IN SELECT * FROM hitl.proposal_gate WHERE proposal_id = prop LOOP
    IF gate.gate_kind = 'ontology' THEN
      INSERT INTO hitl.gate_decision (gate_id, reviewer_id, decision, review_seconds)
      VALUES (gate.gate_id, rev_sme, 'approve', 240);
    END IF;
  END LOOP;
  PERFORM pg_temp.assert(NOT hitl.gates_satisfied(prop),
    'ontology approval alone must not satisfy the control and factual gates');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 4: unauthorised approval does not count';
  ------------------------------------------------------------------
  -- rev_sme has no `control` authority; their approval on a control gate
  -- must be ignored by the quorum count.
  FOR gate IN SELECT * FROM hitl.proposal_gate WHERE proposal_id = prop
               AND gate_kind = 'control' LOOP
    INSERT INTO hitl.gate_decision (gate_id, reviewer_id, decision, review_seconds)
    VALUES (gate.gate_id, rev_sme, 'approve', 30);
  END LOOP;
  PERFORM pg_temp.assert(NOT hitl.gates_satisfied(prop),
    'an approval from a reviewer lacking control authority must not count toward quorum');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 5: full authorised quorum clears';
  ------------------------------------------------------------------
  FOR gate IN SELECT * FROM hitl.proposal_gate WHERE proposal_id = prop LOOP
    IF gate.gate_kind = 'control' THEN
      INSERT INTO hitl.gate_decision (gate_id, reviewer_id, decision, review_seconds)
      VALUES (gate.gate_id, rev_comp, 'approve', 600);
    ELSIF gate.gate_kind = 'factual' THEN
      INSERT INTO hitl.gate_decision (gate_id, reviewer_id, decision, review_seconds)
      VALUES (gate.gate_id, rev_sme, 'approve', 420);
      IF gate.quorum > 1 THEN
        INSERT INTO hitl.gate_decision (gate_id, reviewer_id, decision, review_seconds)
        VALUES (gate.gate_id, rev_owner, 'approve', 380);
      END IF;
    ELSIF gate.gate_kind = 'automation' THEN
      INSERT INTO hitl.gate_decision (gate_id, reviewer_id, decision, review_seconds)
      VALUES (gate.gate_id, rev_owner, 'approve', 500);
    END IF;
  END LOOP;
  PERFORM pg_temp.assert(hitl.gates_satisfied(prop),
    'all gates should now be satisfied by authorised, distinct, non-self reviewers');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 6: merge writes the graph and seals a commit';
  ------------------------------------------------------------------
  SELECT * INTO c FROM hitl.merge_proposal(prop, 'owner@example.com');
  PERFORM pg_temp.assert(c.status = 'sealed', 'merge must produce a sealed commit');
  PERFORM pg_temp.assert(c.content_digest IS NOT NULL, 'commit must carry a content digest');

  SELECT count(*) INTO n FROM kg.node_current WHERE engagement_id = eng;
  PERFORM pg_temp.assert(n = 6, format('expected 6 live nodes, got %s', n));
  SELECT count(*) INTO n FROM kg.edge_current WHERE engagement_id = eng;
  PERFORM pg_temp.assert(n = 6, format('expected 6 live edges, got %s', n));
  PERFORM pg_temp.assert(
    (SELECT bool_and(human_confirmed) FROM kg.edge_current WHERE engagement_id = eng),
    'every merged edge must be marked human_confirmed');
  PERFORM pg_temp.assert(
    (SELECT count(*) FROM kg.evidence WHERE engagement_id = eng) >= 12,
    'evidence rows must be written for every item/source pair');
  PERFORM pg_temp.assert(
    (SELECT count(*) FROM kg.embed_queue WHERE engagement_id = eng) = 12,
    'every new node and edge must be enqueued for embedding');
  PERFORM pg_temp.assert(
    (SELECT status FROM hitl.proposal WHERE proposal_id = prop) = 'merged',
    'proposal must be marked merged');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 7: evidence strength is noisy-OR over sources';
  ------------------------------------------------------------------
  -- proc.quote_to_cash has 2 sources at 0.92 -> 1-(0.08*0.08) = 0.9936
  PERFORM pg_temp.assert(
    kg.evidence_strength(eng,'node','proc.quote_to_cash') >
    kg.evidence_strength(eng,'node','sys.cpq'),
    'two corroborating sources must outrank one stronger single source');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 8: traversal, bounds, and cycle safety';
  ------------------------------------------------------------------
  SELECT count(*) INTO rows_out
    FROM kg.traverse(eng, ARRAY['act.create_quote'], NULL, 3, 500, 'out');
  PERFORM pg_temp.assert(rows_out >= 3,
    format('outward traversal from create_quote should reach >=3 nodes, got %s', rows_out));

  PERFORM pg_temp.assert(
    EXISTS (SELECT 1 FROM kg.dependency_closure(eng,'act.discount_review')
             WHERE node_key = 'sys.cpq'),
    'dependency closure of discount_review must include sys.cpq');

  PERFORM pg_temp.assert(
    EXISTS (SELECT 1 FROM kg.impact_radius(eng,'sys.cpq')
             WHERE node_key = 'act.discount_review'),
    'impact radius of sys.cpq must include discount_review');

  -- Hard bounds must raise, not silently truncate.
  merged_ok := true;
  BEGIN PERFORM * FROM kg.traverse(eng, ARRAY['act.create_quote'], NULL, 99, 500, 'out');
  EXCEPTION WHEN others THEN merged_ok := false; END;
  PERFORM pg_temp.assert(NOT merged_ok, 'max_hops above the ceiling must raise');

  -- Deliberate cycle: the CYCLE clause must terminate.
  INSERT INTO kg.edge (engagement_id, edge_key, src_key, dst_key, edge_type,
                       commit_id, human_confirmed)
  VALUES (eng, kg.make_edge_key('act.discount_review','precedes','act.create_quote'),
          'act.discount_review','act.create_quote','precedes', c.commit_id, true);
  SELECT count(*) INTO rows_out
    FROM kg.traverse(eng, ARRAY['act.create_quote'],
                     ARRAY['precedes']::kg.edge_type[], 6, 500, 'out');
  PERFORM pg_temp.assert(rows_out <= 3,
    format('cycle must be cut by the CYCLE clause, got %s rows', rows_out));

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 9: process_flow orders activities';
  ------------------------------------------------------------------
  PERFORM pg_temp.assert(
    (SELECT activity_key FROM kg.process_flow(eng,'proc.quote_to_cash')
      ORDER BY ordinal LIMIT 1) = 'act.create_quote',
    'create_quote must sort first in the process flow');
  PERFORM pg_temp.assert(
    (SELECT 'ctl.discount_threshold_20' = ANY(gated_by)
       FROM kg.process_flow(eng,'proc.quote_to_cash')
      WHERE activity_key = 'act.create_quote'),
    'process_flow must surface the gating control on create_quote');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 10: hybrid search fuses and reports provenance';
  ------------------------------------------------------------------
  -- Deterministic pseudo-embeddings: enough to exercise the ANN + RRF path.
  qvec := (SELECT array_agg(CASE WHEN i = 1 THEN 1.0 ELSE 0.0 END)::vector(1024)
             FROM generate_series(1,1024) i);
  INSERT INTO kg.node_embedding (node_id, engagement_id, node_type, embed_text,
                                 embedding, model_id)
  SELECT n.node_id, eng, n.node_type, n.label || '. ' || coalesce(n.summary,''),
         (SELECT array_agg(CASE WHEN i = (n.node_id % 1024) + 1 THEN 1.0 ELSE 0.0 END)::vector(1024)
            FROM generate_series(1,1024) i),
         'amazon.titan-embed-text-v2:0'
    FROM kg.node_current n WHERE n.engagement_id = eng;

  INSERT INTO kg.edge_embedding (edge_id, engagement_id, edge_type, embed_text,
                                 embedding, model_id)
  SELECT e.edge_id, eng, e.edge_type,
         e.src_key || ' ' || e.edge_type::text || ' ' || e.dst_key,
         (SELECT array_agg(CASE WHEN i = (e.edge_id % 1024) + 1 THEN 1.0 ELSE 0.0 END)::vector(1024)
            FROM generate_series(1,1024) i),
         'amazon.titan-embed-text-v2:0'
    FROM kg.edge_current e WHERE e.engagement_id = eng;

  SELECT count(*) INTO rows_out FROM kg.hybrid_search(eng, qvec, 10);
  PERFORM pg_temp.assert(rows_out > 0, 'hybrid_search returned nothing');
  PERFORM pg_temp.assert(
    (SELECT bool_and(provenance ? 'lists_matched') FROM kg.hybrid_search(eng, qvec, 10)),
    'every hybrid_search result must carry provenance');

  -- Type filter must actually filter.
  PERFORM pg_temp.assert(
    (SELECT bool_and(node_type = 'activity')
       FROM kg.hybrid_search(eng, qvec, 10, 30, 2, NULL,
                             ARRAY['activity']::kg.node_type[])),
    'node_type filter must be honoured by hybrid_search');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 11: bitemporal point-in-time query';
  ------------------------------------------------------------------
  PERFORM pg_temp.assert(
    (SELECT count(*) FROM kg.node_as_of(eng, now() - interval '1 hour')) = 0,
    'the graph must have been empty an hour ago');
  PERFORM pg_temp.assert(
    (SELECT count(*) FROM kg.node_as_of(eng, now())) = 6,
    'point-in-time at now() must match current state');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 12: drift detection catches a control bypass';
  ------------------------------------------------------------------
  INSERT INTO sor.adapter (engagement_id, adapter_key, system_node_key, kind, mapping)
  VALUES (eng, 'cpq-prod', 'sys.cpq', 'rest_poll',
          '{"activity_field":"status","case_id_field":"quote_id"}');

  -- 30 quotes created. Only 18 went through discount review -> 40% bypass.
  INSERT INTO sor.observation (engagement_id, adapter_id, case_ref, activity_key,
                               raw_activity, actor_role_key, occurred_at)
  SELECT eng, (SELECT adapter_id FROM sor.adapter WHERE adapter_key='cpq-prod'),
         'Q-' || g, 'act.create_quote', 'Quote Created', 'role.sales_rep',
         now() - (g || ' hours')::interval
    FROM generate_series(1,30) g;

  INSERT INTO sor.observation (engagement_id, adapter_id, case_ref, activity_key,
                               raw_activity, actor_role_key, occurred_at)
  SELECT eng, (SELECT adapter_id FROM sor.adapter WHERE adapter_key='cpq-prod'),
         'Q-' || g, 'ctl.discount_threshold_20', 'Deal Desk Approved',
         'role.deal_desk', now() - (g || ' hours')::interval - interval '10 minutes'
    FROM generate_series(1,18) g;

  n := sor.detect_control_bypass(eng, 10, interval '90 days');
  PERFORM pg_temp.assert(n >= 1, 'control bypass detector found nothing on a 40% bypass rate');
  PERFORM pg_temp.assert(
    (SELECT severity FROM sor.drift_signal
      WHERE engagement_id = eng AND drift_kind = 'control_bypass') = 'critical',
    'a 40% control bypass rate must be severity=critical');
  PERFORM pg_temp.assert(
    (SELECT (detail->>'bypass_rate')::numeric FROM sor.drift_signal
      WHERE engagement_id = eng AND drift_kind = 'control_bypass') = 0.4000,
    'bypass rate must be computed as 12/30 = 0.4');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 13: drift dedup does not spam the queue';
  ------------------------------------------------------------------
  PERFORM sor.detect_control_bypass(eng, 10, interval '90 days');
  PERFORM sor.detect_control_bypass(eng, 10, interval '90 days');
  PERFORM pg_temp.assert(
    (SELECT count(*) FROM sor.drift_signal
      WHERE engagement_id = eng AND drift_kind = 'control_bypass') = 1,
    're-detecting the same drift must update, not duplicate');
  PERFORM pg_temp.assert(
    (SELECT occurrences FROM sor.drift_signal
      WHERE engagement_id = eng AND drift_kind = 'control_bypass') = 3,
    'occurrence counter must increment on re-detection');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 14: workflow faithfulness is enforced';
  ------------------------------------------------------------------
  DECLARE wfid bigint; stepid bigint; ok boolean;
  BEGIN
    INSERT INTO wf.workflow (engagement_id, slug, title, pinned_commit_id,
                             root_process_key, authored_by)
    VALUES (eng, 'q2c-discount-approval', 'Q2C Discount Approval',
            c.commit_id, 'proc.quote_to_cash', 'agent:workflow')
    RETURNING workflow_id INTO wfid;

    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction)
    VALUES (wfid, 'check_discount', 1, 'tool', 'Check discount level',
            'Read the quote discount percentage from CPQ.')
    RETURNING step_id INTO stepid;

    -- Unbound step must block publication.
    ok := true;
    BEGIN PERFORM wf.assert_faithful(wfid);
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok, 'a workflow with an unbound step must fail assert_faithful');

    -- Bind to a real graph element -> now it passes.
    INSERT INTO wf.step_binding (step_id, subject_kind, subject_key, relation, pinned_label)
    VALUES (stepid, 'node', 'ctl.discount_threshold_20', 'enforces', '20% Discount Threshold');
    PERFORM wf.assert_faithful(wfid);

    -- Binding to a key that does not exist must fail.
    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction)
    VALUES (wfid, 'phantom', 2, 'tool', 'Phantom step', 'Does something imaginary.')
    RETURNING step_id INTO stepid;
    INSERT INTO wf.step_binding (step_id, subject_kind, subject_key, relation)
    VALUES (stepid, 'node', 'act.does_not_exist', 'implements');
    ok := true;
    BEGIN PERFORM wf.assert_faithful(wfid);
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok, 'binding to a non-existent key must fail assert_faithful');
  END;

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 15: stale-pin drift raised on the next merge';
  ------------------------------------------------------------------
  UPDATE wf.workflow SET status = 'published', published_at = now()
   WHERE engagement_id = eng;

  DECLARE prop2 bigint; g2 record;
  BEGIN
    INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                               agent_name, base_commit_id)
    VALUES (eng, 'add legal review step', 'SoR shows a legal hop we did not model.',
            'agent:engagement', 'engagement', c.commit_id)
    RETURNING proposal_id INTO prop2;
    INSERT INTO hitl.proposal_item (proposal_id, ordinal, op, node_type, subject_key,
                                    payload, source_ids, agent_confidence)
    VALUES (prop2, 1, 'add_node', 'activity', 'act.legal_review',
            '{"label":"Legal Review","summary":"Legal reviews non-standard terms."}',
            ARRAY[src_a], 0.85);
    PERFORM hitl.submit_proposal(prop2);
    FOR g2 IN SELECT * FROM hitl.proposal_gate WHERE proposal_id = prop2 LOOP
      INSERT INTO hitl.gate_decision (gate_id, reviewer_id, decision)
      VALUES (g2.gate_id,
              CASE g2.gate_kind WHEN 'control' THEN rev_comp ELSE rev_sme END,
              'approve');
      IF g2.quorum > 1 THEN
        INSERT INTO hitl.gate_decision (gate_id, reviewer_id, decision)
        VALUES (g2.gate_id, rev_owner, 'approve');
      END IF;
    END LOOP;
    UPDATE hitl.proposal SET status = 'approved' WHERE proposal_id = prop2;
    PERFORM hitl.merge_proposal(prop2, 'owner@example.com');
  END;

  PERFORM pg_temp.assert(
    EXISTS (SELECT 1 FROM sor.drift_signal
             WHERE engagement_id = eng AND drift_kind = 'stale_pin'),
    'merging a new commit must raise stale_pin drift for published workflows');

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 16: rival grader Bradley-Terry fit runs';
  ------------------------------------------------------------------
  DECLARE qid bigint; va bigint; vb bigint; d1 bigint; d2 bigint;
  BEGIN
    SELECT variant_id INTO va FROM trn.retriever_variant WHERE name='rrf-k60-2hop';
    SELECT variant_id INTO vb FROM trn.retriever_variant WHERE name='ann-only';
    INSERT INTO trn.eval_query (engagement_id, question, difficulty, hops_required)
    VALUES (eng, 'Who approves a quote discounted more than 20%?', 'medium', 2)
    RETURNING query_id INTO qid;

    -- 10 mirrored duel pairs, champion wins 8.
    FOR n IN 1..10 LOOP
      INSERT INTO trn.duel (query_id, variant_a, variant_b, presented_first,
                            judge_model, judge_prompt_version, winner)
      VALUES (qid, va, vb, va, 'claude-sonnet-4', 'v1',
              CASE WHEN n <= 8 THEN va ELSE vb END)
      RETURNING duel_id INTO d1;
      INSERT INTO trn.duel (query_id, variant_a, variant_b, presented_first,
                            judge_model, judge_prompt_version, winner,
                            mirror_duel_id, consistent)
      VALUES (qid, va, vb, vb, 'claude-sonnet-4', 'v1',
              CASE WHEN n <= 8 THEN va ELSE vb END, d1, true)
      RETURNING duel_id INTO d2;
      UPDATE trn.duel SET mirror_duel_id = d2, consistent = true WHERE duel_id = d1;
    END LOOP;

    PERFORM pg_temp.assert(
      (SELECT name FROM trn.bradley_terry(50) ORDER BY strength DESC LIMIT 1) = 'rrf-k60-2hop',
      'Bradley-Terry must rank the 80%-winner first');
    PERFORM pg_temp.assert(
      (SELECT bias_rate FROM trn.judge_position_bias('claude-sonnet-4')) = 0.0,
      'position bias rate must be 0 when every mirrored pair agrees');
  END;

  ------------------------------------------------------------------
  RAISE NOTICE '--- setup: traced proposals and three runnable workflows';
  ------------------------------------------------------------------
  DECLARE
    ts_r     uuid := '33333333-3333-3333-3333-333333333331';
    ts_e     uuid := '33333333-3333-3333-3333-333333333332';
    ts_x     uuid := '33333333-3333-3333-3333-333333333333';
    prop_a   bigint; prop_r bigint; prop_e bigint; prop_x bigint;
    item_e1  bigint; item_e2 bigint;
    g_ont    bigint; g_fact bigint;
    d1       hitl.gate_decision;
    d2       hitl.gate_decision;
    wf_run   bigint; wf_fail bigint; wf_human bigint;
    s1 bigint; s2 bigint; s3 bigint; s4 bigint;
    f1 bigint; f2 bigint; f3 bigint; h1 bigint;
    run1 bigint; run2 bigint; run3 bigint; run4 bigint;
    runa bigint; runb bigint; runc bigint;
    rs_c bigint; rs_h bigint; rs_id bigint;
    r_rec  wf.run;
    rs_rec wf.run_step;
    w      wf.workflow;
    ok     boolean;
    n2     int;
  BEGIN
    -- Three agent sessions whose labels the gate is about to decide. Only
    -- the human gate may write trn.trace_session.outcome (011:84-89), so
    -- these start at 'pending' and stay there until a reviewer acts.
    INSERT INTO trn.trace_session (session_id, engagement_id, agent_name,
                                   agent_runtime_arn, model_id, base_commit_id,
                                   task_kind, task_input)
    VALUES
      (ts_r, eng, 'engagement',
       'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-engagement',
       'claude-sonnet-4', genesis, 'map_workflow', '{"ask":"map the dunning flow"}'),
      (ts_e, eng, 'engagement',
       'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-engagement',
       'claude-sonnet-4', genesis, 'map_workflow', '{"ask":"map credit checks"}'),
      (ts_x, eng, 'engagement',
       'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-engagement',
       'claude-sonnet-4', genesis, 'map_workflow', '{"ask":"map SLA expiry"}');

    INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                               agent_name, base_commit_id)
    VALUES (eng, 'add dispute review', 'Interview mentions a dispute hop.',
            'agent:engagement', 'engagement', c.commit_id)
    RETURNING proposal_id INTO prop_a;
    INSERT INTO hitl.proposal_item (proposal_id, ordinal, op, node_type, subject_key,
                                    payload, source_ids, agent_confidence)
    VALUES (prop_a, 1, 'add_node', 'activity', 'act.dispute_review',
            '{"label":"Dispute Review","summary":"AR reviews disputed invoices."}',
            ARRAY[src_a], 0.80);

    INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                               agent_name, base_commit_id, trace_session_id)
    VALUES (eng, 'add a step nobody performs', 'Weakly grounded inference.',
            'agent:engagement', 'engagement', c.commit_id, ts_r)
    RETURNING proposal_id INTO prop_r;
    INSERT INTO hitl.proposal_item (proposal_id, ordinal, op, node_type, subject_key,
                                    payload, source_ids, agent_confidence)
    VALUES (prop_r, 1, 'add_node', 'activity', 'act.bogus_step',
            '{"label":"Bogus Step","summary":"Nothing in the SoR looks like this."}',
            ARRAY[src_a], 0.55);

    INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                               agent_name, base_commit_id, trace_session_id)
    VALUES (eng, 'add credit check and dunning', 'Two candidate activities.',
            'agent:engagement', 'engagement', c.commit_id, ts_e)
    RETURNING proposal_id INTO prop_e;
    INSERT INTO hitl.proposal_item (proposal_id, ordinal, op, node_type, subject_key,
                                    payload, source_ids, agent_confidence)
    VALUES (prop_e, 1, 'add_node', 'activity', 'act.credit_check',
            '{"label":"Credit Check","summary":"Someone checks customer credit."}',
            ARRAY[src_a], 0.72)
    RETURNING item_id INTO item_e1;
    INSERT INTO hitl.proposal_item (proposal_id, ordinal, op, node_type, subject_key,
                                    payload, source_ids, agent_confidence)
    VALUES (prop_e, 2, 'add_node', 'activity', 'act.dunning',
            '{"label":"Dunning","summary":"Collections chases overdue invoices."}',
            ARRAY[src_a], 0.61)
    RETURNING item_id INTO item_e2;

    INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                               agent_name, base_commit_id, trace_session_id)
    VALUES (eng, 'proposal nobody reviews', 'Left to blow its SLA.',
            'agent:engagement', 'engagement', c.commit_id, ts_x)
    RETURNING proposal_id INTO prop_x;
    INSERT INTO hitl.proposal_item (proposal_id, ordinal, op, node_type, subject_key,
                                    payload, source_ids, agent_confidence)
    VALUES (prop_x, 1, 'add_node', 'activity', 'act.sla_probe',
            '{"label":"SLA Probe","summary":"Placeholder activity."}',
            ARRAY[src_a], 0.70);

    ------------------------------------------------------------------
    RAISE NOTICE '--- TEST 17: record_decision enforces authority, supersedes, drives status';
    ------------------------------------------------------------------
    PERFORM hitl.submit_proposal(prop_a);
    SELECT gate_id INTO g_ont FROM hitl.proposal_gate
     WHERE proposal_id = prop_a AND gate_kind = 'ontology';
    PERFORM pg_temp.assert(g_ont IS NOT NULL,
      'the catch-all ontology gate must be on every proposal');

    -- Unknown principal. gates_satisfied would have silently not counted this;
    -- record_decision has to tell the caller.
    ok := true;
    BEGIN PERFORM hitl.record_decision(g_ont, 'ghost@example.com', 'approve');
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok,
      'a decision from an unregistered principal must raise');

    -- Registered, but holds factual/automation authority, not ontology.
    ok := true;
    BEGIN PERFORM hitl.record_decision(g_ont, 'owner@example.com', 'approve');
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok,
      'a reviewer without authority for this gate kind must raise, not quietly '
      'record a decision that can never count');

    SELECT * INTO d1 FROM hitl.record_decision(g_ont, 'sme@example.com', 'approve',
                                               'ontology looks right', '{}'::jsonb, 210);
    PERFORM pg_temp.assert(
      (SELECT status FROM hitl.proposal WHERE proposal_id = prop_a) = 'in_review',
      'the first approval must move the proposal from submitted to in_review');
    PERFORM pg_temp.assert(
      (SELECT cleared_at FROM hitl.proposal_gate WHERE gate_id = g_ont) IS NOT NULL,
      'a gate that has reached quorum must be stamped cleared_at');
    PERFORM pg_temp.assert(d1.review_seconds = 210,
      'review_seconds must be persisted -- it is the rubber-stamp detector');

    -- The reviewer changes their mind. B19: the supersede is a three-step
    -- dance around the partial unique index and the non-deferrable self-FK.
    SELECT * INTO d2 FROM hitl.record_decision(g_ont, 'sme@example.com', 'approve',
                                               'same call, better note', '{}'::jsonb, 95);
    PERFORM pg_temp.assert(
      (SELECT count(*) FROM hitl.gate_decision
        WHERE gate_id = g_ont AND reviewer_id = rev_sme) = 2,
      'decisions are append-only: the superseded row must survive');
    PERFORM pg_temp.assert(
      (SELECT count(*) FROM hitl.gate_decision
        WHERE gate_id = g_ont AND reviewer_id = rev_sme AND superseded_by IS NULL) = 1,
      'exactly one live decision per (gate, reviewer) may remain');
    PERFORM pg_temp.assert(
      (SELECT superseded_by FROM hitl.gate_decision WHERE decision_id = d1.decision_id)
        = d2.decision_id,
      'the superseded row must end up pointing at the new decision, not at itself');

    FOR gate IN SELECT * FROM hitl.proposal_gate
                 WHERE proposal_id = prop_a AND cleared_at IS NULL LOOP
      PERFORM hitl.record_decision(gate.gate_id,
        CASE gate.gate_kind WHEN 'control'    THEN 'compliance@example.com'
                            WHEN 'automation' THEN 'owner@example.com'
                            ELSE 'sme@example.com' END, 'approve');
      IF gate.quorum > 1 THEN
        PERFORM hitl.record_decision(gate.gate_id, 'owner@example.com', 'approve');
      END IF;
    END LOOP;
    PERFORM pg_temp.assert(
      (SELECT status FROM hitl.proposal WHERE proposal_id = prop_a) = 'approved',
      'a proposal whose every gate has quorum must land in approved');

    -- Reject is terminal, and labels the trace on the spot.
    PERFORM hitl.submit_proposal(prop_r);
    SELECT gate_id INTO g_fact FROM hitl.proposal_gate
     WHERE proposal_id = prop_r AND gate_kind = 'factual' LIMIT 1;
    PERFORM hitl.record_decision(g_fact, 'sme@example.com', 'reject',
                                 'the SoR shows nothing like this');
    PERFORM pg_temp.assert(
      (SELECT status FROM hitl.proposal WHERE proposal_id = prop_r) = 'rejected',
      'a reject must move the proposal to rejected');
    PERFORM pg_temp.assert(
      (SELECT outcome FROM trn.trace_session WHERE session_id = ts_r) = 'rejected'
      AND (SELECT label_source FROM trn.trace_session WHERE session_id = ts_r) = 'hitl_gate'
      AND (SELECT label_proposal_id FROM trn.trace_session WHERE session_id = ts_r) = prop_r,
      'a rejected proposal must label its trace from the human gate');
    PERFORM pg_temp.assert(NOT hitl.gates_satisfied(prop_r),
      'a live reject must keep gates_satisfied false');

    ------------------------------------------------------------------
    RAISE NOTICE '--- TEST 18: the reviewer edit survives into the graph and the label';
    ------------------------------------------------------------------
    PERFORM hitl.submit_proposal(prop_e);

    PERFORM hitl.edit_item(item_e1, 'sme@example.com',
      '{"label":"Credit Check (Deal Desk)","summary":"Deal desk verifies customer credit before booking."}'::jsonb);
    PERFORM pg_temp.assert(
      (SELECT item_status FROM hitl.proposal_item WHERE item_id = item_e1) = 'edited',
      'an edited item must be marked edited');
    PERFORM pg_temp.assert(
      (SELECT original_payload->>'label' FROM hitl.proposal_item WHERE item_id = item_e1)
        = 'Credit Check',
      'the agent''s original payload must be retained -- it IS the training delta');
    PERFORM pg_temp.assert(
      (SELECT edited_by FROM hitl.proposal_item WHERE item_id = item_e1) = 'sme@example.com'
      AND (SELECT edited_at FROM hitl.proposal_item WHERE item_id = item_e1) IS NOT NULL,
      'the edit must be attributed');

    -- The second item is dropped through the decision's per-item verdicts.
    FOR gate IN SELECT * FROM hitl.proposal_gate WHERE proposal_id = prop_e LOOP
      PERFORM hitl.record_decision(gate.gate_id,
        CASE gate.gate_kind WHEN 'control'    THEN 'compliance@example.com'
                            WHEN 'automation' THEN 'owner@example.com'
                            ELSE 'sme@example.com' END,
        'approve', 'keeping the credit check, dropping dunning',
        jsonb_build_object(item_e2::text, jsonb_build_object('verdict', 'drop')));
      IF gate.quorum > 1 THEN
        PERFORM hitl.record_decision(gate.gate_id, 'owner@example.com', 'approve');
      END IF;
    END LOOP;
    PERFORM pg_temp.assert(
      (SELECT item_status FROM hitl.proposal_item WHERE item_id = item_e2) = 'dropped',
      'a drop verdict must mark the item dropped');
    PERFORM pg_temp.assert(
      (SELECT status FROM hitl.proposal WHERE proposal_id = prop_e) = 'approved',
      'the proposal must reach approved once every gate clears');

    PERFORM hitl.merge_proposal(prop_e, 'owner@example.com');
    PERFORM hitl.apply_trace_label(prop_e);

    PERFORM pg_temp.assert(
      (SELECT label FROM kg.node_current
        WHERE engagement_id = eng AND node_key = 'act.credit_check')
        = 'Credit Check (Deal Desk)',
      'the merge must write the reviewer''s edited payload, not the agent''s');
    PERFORM pg_temp.assert(
      NOT EXISTS (SELECT 1 FROM kg.node_current
                   WHERE engagement_id = eng AND node_key = 'act.dunning'),
      'a dropped item must never reach the graph');
    PERFORM pg_temp.assert(
      (SELECT outcome FROM trn.trace_session WHERE session_id = ts_e) = 'corrected',
      'a merge that a human had to correct is labelled corrected, not accepted');

    ------------------------------------------------------------------
    RAISE NOTICE '--- TEST 19: publish_workflow is the only publish path, and is gated';
    ------------------------------------------------------------------
    INSERT INTO wf.workflow (engagement_id, slug, title, pinned_commit_id,
                             root_process_key, authored_by, runnable_by)
    VALUES (eng, 'q2c-discount-run', 'Q2C Discount Run', c.commit_id,
            'proc.quote_to_cash', 'agent:workflow', ARRAY['prodops-team'])
    RETURNING workflow_id INTO wf_run;

    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction,
                         tool_name, tool_args, on_failure)
    VALUES (wf_run, 'fetch_quote', 1, 'tool', 'Fetch the quote',
            'Read the quote and its discount percentage from CPQ.',
            'kg_get_node', '{"node_key":"sys.cpq"}'::jsonb, 'halt')
    RETURNING step_id INTO s1;
    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction, branches)
    VALUES (wf_run, 'route', 2, 'decision', 'Route on discount level',
            'Above the 20% threshold the deal desk must approve.',
            '[{"when":"$.fetch_quote.discount_pct > 20","goto":"approve_discount"},
              {"else":"notify_rep"}]'::jsonb)
    RETURNING step_id INTO s2;
    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction,
                         human_prompt, human_schema, timeout_seconds)
    VALUES (wf_run, 'approve_discount', 3, 'human', 'Approve the discount',
            'Deal desk decides whether this discount may stand.',
            'Approve this discount?', '{"approved":"boolean"}'::jsonb, 60)
    RETURNING step_id INTO s3;
    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction)
    VALUES (wf_run, 'notify_rep', 4, 'notify', 'Tell the rep',
            'Send the outcome back to the sales rep.')
    RETURNING step_id INTO s4;

    -- assert_faithful fires from inside the function, so an unfaithful
    -- workflow cannot be published by a caller who simply forgot to check.
    ok := true;
    BEGIN PERFORM wf.publish_workflow(wf_run, 'owner@example.com');
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok, 'publishing with an unbound step must raise');
    PERFORM pg_temp.assert(
      (SELECT status FROM wf.workflow WHERE workflow_id = wf_run) = 'draft',
      'a refused publish must leave the workflow in draft');

    INSERT INTO wf.step_binding (step_id, subject_kind, subject_key, relation, pinned_label)
    VALUES (s1, 'node', 'sys.cpq', 'depends_on', 'CPQ'),
           (s2, 'node', 'ctl.discount_threshold_20', 'enforces', '20% Discount Threshold'),
           (s3, 'node', 'role.deal_desk', 'implements', 'Deal Desk Analyst');

    SELECT * INTO w FROM wf.publish_workflow(wf_run, 'owner@example.com');
    PERFORM pg_temp.assert(w.status = 'published', 'a faithful workflow must publish');
    PERFORM pg_temp.assert(w.published_by = 'owner@example.com' AND w.published_at IS NOT NULL,
      'publish must stamp who and when');
    PERFORM pg_temp.assert(
      w.pinned_digest = (SELECT content_digest FROM kg.commit WHERE commit_id = c.commit_id),
      'publish must pin the commit digest, not just the commit id');

    ok := true;
    BEGIN PERFORM wf.publish_workflow(wf_run, 'owner@example.com');
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok, 'republishing a published workflow must raise');

    -- A single-human-step workflow, and one whose steps exercise every
    -- on_failure policy. The failure one stays draft for TEST 20's guard.
    INSERT INTO wf.workflow (engagement_id, slug, title, pinned_commit_id,
                             root_process_key, authored_by)
    VALUES (eng, 'q2c-human-hold', 'Q2C Human Hold', c.commit_id,
            'proc.quote_to_cash', 'agent:workflow')
    RETURNING workflow_id INTO wf_human;
    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction,
                         human_prompt, timeout_seconds)
    VALUES (wf_human, 'hold', 1, 'human', 'Hold for a person',
            'Wait for a human decision.', 'Proceed?', 30)
    RETURNING step_id INTO h1;
    INSERT INTO wf.step_binding (step_id, subject_kind, subject_key, relation)
    VALUES (h1, 'node', 'act.discount_review', 'implements');
    PERFORM wf.publish_workflow(wf_human, 'owner@example.com');

    INSERT INTO wf.workflow (engagement_id, slug, title, pinned_commit_id,
                             root_process_key, authored_by)
    VALUES (eng, 'q2c-failure-policies', 'Q2C Failure Policies', c.commit_id,
            'proc.quote_to_cash', 'agent:workflow')
    RETURNING workflow_id INTO wf_fail;
    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction,
                         tool_name, on_failure, timeout_seconds)
    VALUES (wf_fail, 'flaky', 1, 'tool', 'Flaky read', 'Read from a flaky system.',
            'kg_get_node', 'retry', 30)
    RETURNING step_id INTO f1;
    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction,
                         tool_name, on_failure)
    VALUES (wf_fail, 'optional', 2, 'tool', 'Optional enrichment',
            'Enrich the case; not worth stopping for.', 'kg_search', 'skip')
    RETURNING step_id INTO f2;
    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title, instruction,
                         tool_name, on_failure)
    VALUES (wf_fail, 'critical', 3, 'tool', 'Critical write',
            'Record the outcome; a person must handle failures.',
            'kg_propose_change', 'escalate')
    RETURNING step_id INTO f3;
    INSERT INTO wf.step_binding (step_id, subject_kind, subject_key, relation)
    VALUES (f1, 'node', 'sys.cpq', 'depends_on'),
           (f2, 'node', 'act.create_quote', 'implements'),
           (f3, 'node', 'act.discount_review', 'records_to');

    ------------------------------------------------------------------
    RAISE NOTICE '--- TEST 20: runs start only from published workflows and advance in order';
    ------------------------------------------------------------------
    ok := true;
    BEGIN PERFORM wf.start_run(wf_fail, 'ops@example.com');
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok, 'starting a draft workflow must raise');

    ok := true;
    BEGIN PERFORM wf.start_run(wf_run, 'ops@example.com', '{}'::jsonb, ARRAY['interns']);
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok,
      'runnable_by is an ACL: a principal outside it must not start the run');

    SELECT * INTO r_rec FROM wf.start_run(wf_run, 'ops@example.com',
                                          '{"quote_id":"Q-1001"}'::jsonb,
                                          ARRAY['prodops-team']);
    run1 := r_rec.run_id;
    PERFORM pg_temp.assert(r_rec.status = 'pending', 'a new run starts pending');
    PERFORM pg_temp.assert(r_rec.current_step_id = s1,
      'the cursor starts on the lowest-ordinal step');
    PERFORM pg_temp.assert(r_rec.context->'input'->>'quote_id' = 'Q-1001',
      'run input must be reachable in the context as $.input');

    SELECT * INTO rs_rec FROM wf.begin_step(run1);
    PERFORM pg_temp.assert(rs_rec.attempt = 1 AND rs_rec.status = 'running',
      'begin_step opens attempt 1 in running');
    PERFORM pg_temp.assert(
      (SELECT status FROM wf.run WHERE run_id = run1) = 'running',
      'beginning a step moves the run to running');

    ok := true;
    BEGIN PERFORM wf.begin_step(run1);
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok,
      'a second begin_step while a step is open must raise -- that guard is the '
      'runner''s mutex between the tick and an API-triggered advance');

    PERFORM wf.complete_step(rs_rec.run_step_id, '{"discount_pct":35}'::jsonb);
    PERFORM pg_temp.assert(
      (SELECT context->'fetch_quote'->>'discount_pct' FROM wf.run WHERE run_id = run1) = '35',
      'step output must accumulate in run.context under the step_key');
    PERFORM pg_temp.assert(
      (SELECT current_step_id FROM wf.run WHERE run_id = run1) = s2,
      'completing a step advances the cursor to the next ordinal');

    SELECT * INTO rs_rec FROM wf.begin_step(run1);
    PERFORM wf.complete_step(rs_rec.run_step_id, '{"branch":"approve_discount"}'::jsonb);
    PERFORM pg_temp.assert(
      (SELECT current_step_id FROM wf.run WHERE run_id = run1) = s3,
      'the decision step advances to the human step');

    ------------------------------------------------------------------
    RAISE NOTICE '--- TEST 21: human steps wait, respond, branch, and abort';
    ------------------------------------------------------------------
    SELECT * INTO rs_rec FROM wf.begin_step(run1);
    SELECT * INTO rs_rec FROM wf.await_human(rs_rec.run_step_id, 'owner@example.com');
    PERFORM pg_temp.assert(rs_rec.status = 'awaiting_human',
      'await_human parks the step');
    PERFORM pg_temp.assert(
      (SELECT status FROM wf.run WHERE run_id = run1) = 'awaiting_human',
      'and parks the run with it');
    PERFORM pg_temp.assert(
      EXISTS (SELECT 1 FROM wf.run_step
               WHERE status = 'awaiting_human' AND awaiting_principal = 'owner@example.com'),
      'the waiting step must be visible through the review-queue partial index');

    SELECT * INTO r_rec FROM wf.respond_human(rs_rec.run_step_id, 'owner@example.com',
                                              '{"approved":true}'::jsonb, 'approve');
    PERFORM pg_temp.assert(r_rec.current_step_id = s4,
      'approving a human step advances the run');
    PERFORM pg_temp.assert(
      (SELECT context->'approve_discount'->>'approved' FROM wf.run WHERE run_id = run1) = 'true',
      'the human response lands in the context like any other step output');
    PERFORM pg_temp.assert(
      (SELECT responded_by FROM wf.run_step WHERE run_step_id = rs_rec.run_step_id)
        = 'owner@example.com',
      'the responder must be recorded');

    SELECT * INTO rs_rec FROM wf.begin_step(run1);
    SELECT * INTO r_rec FROM wf.complete_step(rs_rec.run_step_id,
                                              '{"delivered":false,"mode":"log_only"}'::jsonb);
    PERFORM pg_temp.assert(
      r_rec.status = 'succeeded' AND r_rec.current_step_id IS NULL
      AND r_rec.finished_at IS NOT NULL,
      'completing the last step must finish the run');

    -- A decision branch jumps by step_key rather than falling through.
    SELECT * INTO r_rec FROM wf.start_run(wf_run, 'ops@example.com',
                                          '{"quote_id":"Q-1002"}'::jsonb,
                                          ARRAY['prodops-team']);
    run2 := r_rec.run_id;
    SELECT * INTO rs_rec FROM wf.begin_step(run2);
    PERFORM wf.complete_step(rs_rec.run_step_id, '{"discount_pct":5}'::jsonb);
    SELECT * INTO rs_rec FROM wf.begin_step(run2);
    SELECT * INTO r_rec FROM wf.complete_step(rs_rec.run_step_id,
                                              '{"branch":"notify_rep"}'::jsonb, 'notify_rep');
    PERFORM pg_temp.assert(r_rec.current_step_id = s4,
      'a goto must move the cursor to its target, skipping the human step');

    SELECT * INTO rs_rec FROM wf.begin_step(run2);
    ok := true;
    BEGIN PERFORM wf.complete_step(rs_rec.run_step_id, '{}'::jsonb, 'hold');
    EXCEPTION WHEN others THEN ok := false; END;
    PERFORM pg_temp.assert(NOT ok,
      'a goto naming a step of a different workflow must raise');
    SELECT * INTO r_rec FROM wf.complete_step(rs_rec.run_step_id, '{"delivered":false}'::jsonb);
    PERFORM pg_temp.assert(r_rec.status = 'succeeded', 'run 2 finishes after its notify step');

    -- Abort from the review queue cancels the run and closes its open steps.
    SELECT * INTO r_rec FROM wf.start_run(wf_human, 'ops@example.com');
    run3 := r_rec.run_id;
    SELECT * INTO rs_rec FROM wf.begin_step(run3);
    PERFORM wf.await_human(rs_rec.run_step_id, NULL);
    SELECT * INTO r_rec FROM wf.respond_human(rs_rec.run_step_id, 'owner@example.com',
                                              '{"reason":"duplicate run"}'::jsonb, 'abort');
    PERFORM pg_temp.assert(r_rec.status = 'cancelled', 'abort must cancel the run');
    PERFORM pg_temp.assert(r_rec.error->>'cancelled_by' = 'owner@example.com',
      'the cancellation must name who did it');
    PERFORM pg_temp.assert(
      (SELECT status FROM wf.run_step WHERE run_step_id = rs_rec.run_step_id) = 'cancelled',
      'cancelling a run must close its open steps');
    PERFORM pg_temp.assert(
      (SELECT human_response->>'reason' FROM wf.run_step
        WHERE run_step_id = rs_rec.run_step_id) = 'duplicate run',
      'the operator''s answer is recorded even when it aborts the run');

    ------------------------------------------------------------------
    RAISE NOTICE '--- TEST 22: failure policies, the timeout sweep, and the expiry sweep';
    ------------------------------------------------------------------
    PERFORM wf.publish_workflow(wf_fail, 'owner@example.com');

    SELECT * INTO r_rec FROM wf.start_run(wf_fail, 'ops@example.com');
    runa := r_rec.run_id;
    SELECT * INTO rs_rec FROM wf.begin_step(runa);
    SELECT * INTO r_rec FROM wf.fail_step(rs_rec.run_step_id,
                                          '{"error":"CPQ timed out"}'::jsonb, 2);
    PERFORM pg_temp.assert(r_rec.status = 'running' AND r_rec.current_step_id = f1,
      'on_failure=retry leaves the run running on the same step');
    SELECT * INTO rs_rec FROM wf.begin_step(runa);
    PERFORM pg_temp.assert(rs_rec.attempt = 2, 'the retry must open attempt 2');
    SELECT * INTO r_rec FROM wf.fail_step(rs_rec.run_step_id,
                                          '{"error":"CPQ timed out"}'::jsonb, 2);
    PERFORM pg_temp.assert(r_rec.status = 'failed'
                             AND r_rec.error->>'error' = 'CPQ timed out',
      'exhausting the retry ceiling fails the run, carrying the error verbatim');

    SELECT * INTO r_rec FROM wf.start_run(wf_fail, 'ops@example.com');
    runb := r_rec.run_id;
    SELECT * INTO rs_rec FROM wf.begin_step(runb);
    PERFORM wf.complete_step(rs_rec.run_step_id, '{"ok":true}'::jsonb);
    SELECT * INTO rs_rec FROM wf.begin_step(runb);
    SELECT * INTO r_rec FROM wf.fail_step(rs_rec.run_step_id,
                                          '{"error":"enrichment source down"}'::jsonb);
    PERFORM pg_temp.assert(r_rec.status = 'running' AND r_rec.current_step_id = f3,
      'on_failure=skip advances past the failed step');
    SELECT * INTO rs_rec FROM wf.begin_step(runb);
    SELECT * INTO r_rec FROM wf.fail_step(rs_rec.run_step_id,
                                          '{"error":"needs a person"}'::jsonb);
    PERFORM pg_temp.assert(r_rec.status = 'awaiting_human',
      'on_failure=escalate parks the run on a human');
    SELECT run_step_id INTO rs_id FROM wf.run_step
     WHERE run_id = runb AND status = 'awaiting_human';
    PERFORM pg_temp.assert(
      (SELECT input->'escalated_error'->>'error' FROM wf.run_step WHERE run_step_id = rs_id)
        = 'needs a person',
      'the escalated attempt must carry the failure that caused it');
    SELECT * INTO r_rec FROM wf.respond_human(rs_id, 'owner@example.com',
                                              '{"fixed":true}'::jsonb, 'retry');
    PERFORM pg_temp.assert(r_rec.status = 'running' AND r_rec.current_step_id = f3,
      'retry from an escalation re-runs the same step');
    SELECT * INTO rs_rec FROM wf.begin_step(runb);
    PERFORM pg_temp.assert(rs_rec.attempt = 3, 'the re-run is a fresh attempt');
    SELECT * INTO r_rec FROM wf.complete_step(rs_rec.run_step_id, '{"ok":true}'::jsonb);
    PERFORM pg_temp.assert(r_rec.status = 'succeeded',
      'the run finishes once the escalated step is done');

    -- One overdue running step and one overdue awaiting_human step. Only the
    -- first is eligible: docs/10 §2 promises the workflow waits for a person.
    SELECT * INTO r_rec FROM wf.start_run(wf_fail, 'ops@example.com');
    runc := r_rec.run_id;
    SELECT * INTO rs_rec FROM wf.begin_step(runc);
    rs_c := rs_rec.run_step_id;
    UPDATE wf.run_step SET started_at = now() - interval '1 hour' WHERE run_step_id = rs_c;

    SELECT * INTO r_rec FROM wf.start_run(wf_human, 'ops@example.com');
    run4 := r_rec.run_id;
    SELECT * INTO rs_rec FROM wf.begin_step(run4);
    rs_h := rs_rec.run_step_id;
    PERFORM wf.await_human(rs_h, NULL);
    UPDATE wf.run_step SET started_at = now() - interval '1 hour' WHERE run_step_id = rs_h;

    n2 := wf.timeout_steps();
    PERFORM pg_temp.assert(n2 = 1,
      format('exactly one overdue running step must time out, got %s', n2));
    PERFORM pg_temp.assert(
      (SELECT status FROM wf.run_step WHERE run_step_id = rs_c) = 'failed',
      'the overdue running step must be failed by the sweep');
    PERFORM pg_temp.assert(
      (SELECT status FROM wf.run_step WHERE run_step_id = rs_h) = 'awaiting_human',
      'an awaiting_human step must NEVER be timed out');

    -- The hourly expiry sweep. Deliberately does not label the trace.
    PERFORM hitl.submit_proposal(prop_x);
    UPDATE hitl.proposal SET expires_at = now() - interval '1 day'
     WHERE proposal_id = prop_x;
    n2 := hitl.expire_proposals();
    PERFORM pg_temp.assert(n2 = 1,
      format('exactly the one overdue proposal must expire, got %s', n2));
    PERFORM pg_temp.assert(
      (SELECT status FROM hitl.proposal WHERE proposal_id = prop_x) = 'expired',
      'an SLA-breached proposal must be expired');
    PERFORM pg_temp.assert(
      (SELECT outcome FROM trn.trace_session WHERE session_id = ts_x) = 'pending',
      'expiry is an SLA breach, not a human judgment: the trace outcome stays '
      'pending so the session never enters trn.sft_export');
  END;

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 23: fde_agent may ingest evidence chunks, never rewrite them';
  ------------------------------------------------------------------
  DECLARE
    v_chunk bigint;
    n_enq   int;
    ok      boolean;
  BEGIN
    EXECUTE 'SET LOCAL ROLE fde_agent';

    INSERT INTO kg.chunk (engagement_id, source_id, ordinal, content, anchor_keys)
    VALUES (eng, src_b, 1,
            'Quotes discounted more than 20 percent route to the deal desk before send.',
            ARRAY['ctl.discount_threshold_20','act.discount_review'])
    RETURNING chunk_id INTO v_chunk;

    INSERT INTO kg.embed_queue (engagement_id, subject_kind, subject_id)
    VALUES (eng, 'chunk', v_chunk)
    ON CONFLICT (subject_kind, subject_id) DO NOTHING;

    INSERT INTO kg.embed_queue (engagement_id, subject_kind, subject_id)
    VALUES (eng, 'chunk', v_chunk)
    ON CONFLICT (subject_kind, subject_id) DO NOTHING;
    GET DIAGNOSTICS n_enq = ROW_COUNT;

    ok := true;
    BEGIN
      UPDATE kg.chunk SET content = 'rewritten after the reviewer read it'
       WHERE chunk_id = v_chunk;
    EXCEPTION WHEN insufficient_privilege THEN ok := false;
    END;

    EXECUTE 'RESET ROLE';

    PERFORM pg_temp.assert(v_chunk IS NOT NULL,
      'fde_agent must be able to INSERT kg.chunk (014)');
    PERFORM pg_temp.assert(n_enq = 0,
      're-enqueuing an already queued chunk must be a no-op, not an error');
    PERFORM pg_temp.assert(
      (SELECT count(*) FROM kg.embed_queue
        WHERE subject_kind = 'chunk' AND subject_id = v_chunk) = 1,
      'exactly one embed_queue row per chunk');
    PERFORM pg_temp.assert(NOT ok,
      'fde_agent must NOT be able to UPDATE kg.chunk -- ingested evidence is '
      'immutable, or an agent could rewrite the text a human signed off on');
    PERFORM pg_temp.assert(
      (SELECT content FROM kg.chunk WHERE chunk_id = v_chunk) LIKE 'Quotes discounted%',
      'the refused UPDATE must have left the evidence untouched');
  END;

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 24: observation dedup absorbs at-least-once delivery';
  ------------------------------------------------------------------
  DECLARE
    adp   bigint;
    n_ins int;
  BEGIN
    SELECT adapter_id INTO adp FROM sor.adapter
     WHERE engagement_id = eng AND adapter_key = 'cpq-prod';

    INSERT INTO sor.observation (engagement_id, adapter_id, case_ref, activity_key,
                                 raw_activity, occurred_at, dedup_key)
    VALUES (eng, adp, 'Q-9001', 'act.create_quote', 'Quote Created',
            now() - interval '2 hours', 'sha256:cafebabe')
    ON CONFLICT (adapter_id, dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING;

    -- The same record redelivered: SQS at-least-once, a crash-resumed batch,
    -- or a backfill overlapping the live poll window.
    INSERT INTO sor.observation (engagement_id, adapter_id, case_ref, activity_key,
                                 raw_activity, occurred_at, dedup_key)
    VALUES (eng, adp, 'Q-9001', 'act.create_quote', 'Quote Created',
            now() - interval '2 hours', 'sha256:cafebabe')
    ON CONFLICT (adapter_id, dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING;
    GET DIAGNOSTICS n_ins = ROW_COUNT;

    PERFORM pg_temp.assert(n_ins = 0,
      'a redelivered observation must not insert a second row -- the detectors '
      'compute rates over counted rows, so a duplicate moves the severity');
    PERFORM pg_temp.assert(
      (SELECT count(*) FROM sor.observation
        WHERE adapter_id = adp AND dedup_key = 'sha256:cafebabe') = 1,
      'exactly one row per (adapter_id, dedup_key)');

    -- Hand-written rows leave dedup_key NULL and stay unconstrained, which is
    -- what the partial predicate is for: TEST 12's 48 observations are intact.
    INSERT INTO sor.observation (engagement_id, adapter_id, case_ref, activity_key,
                                 raw_activity, occurred_at)
    VALUES (eng, adp, 'Q-9002', 'act.create_quote', 'Quote Created', now()),
           (eng, adp, 'Q-9002', 'act.create_quote', 'Quote Created', now());
    PERFORM pg_temp.assert(
      (SELECT count(*) FROM sor.observation
        WHERE adapter_id = adp AND case_ref = 'Q-9002') = 2,
      'the partial unique index must not constrain rows with a NULL dedup_key');
  END;

  ------------------------------------------------------------------
  RAISE NOTICE '--- TEST 25: closure and impact radius answer as of a commit';
  ------------------------------------------------------------------
  DECLARE
    eng2 uuid := '22222222-2222-2222-2222-222222222222';
    t1   timestamptz := now() - interval '2 hours';
    t2   timestamptz := now() - interval '1 hour';
    ac1  bigint; ac2 bigint;
  BEGIN
    -- This whole file is one transaction, so now() never moves: a retirement
    -- written through hitl.merge_proposal would land at the same instant as
    -- the fact it retires and there would be no interval to query. The two
    -- commits below therefore carry explicit valid times -- t1 stands for the
    -- first commit's seal, t2 for the second's -- on their own engagement so
    -- nothing above is disturbed.
    INSERT INTO kg.commit (status, engagement_id, title, authored_by, sealed_by,
                           sealed_at, content_digest)
    VALUES ('sealed', eng2, 'asof: initial mapping', 'bootstrap', 'bootstrap', t1, 'd1')
    RETURNING commit_id INTO ac1;
    INSERT INTO kg.commit (parent_id, status, engagement_id, title, authored_by,
                           sealed_by, sealed_at, content_digest)
    VALUES (ac1, 'sealed', eng2, 'asof: retire the dependency', 'bootstrap',
            'bootstrap', t2, 'd2')
    RETURNING commit_id INTO ac2;

    INSERT INTO kg.node (engagement_id, node_key, node_type, label, valid_from, commit_id)
    VALUES (eng2, 'act.settle',  'activity', 'Settle Invoice', t1, ac1),
           (eng2, 'sys.ledger',  'system',   'General Ledger', t1, ac1);
    INSERT INTO kg.edge (engagement_id, edge_key, src_key, dst_key, edge_type,
                         valid_from, commit_id, human_confirmed)
    VALUES (eng2, kg.make_edge_key('act.settle','depends_on','sys.ledger'),
            'act.settle', 'sys.ledger', 'depends_on', t1, ac1, true);

    -- Commit 2 retires the dependency.
    PERFORM kg.close_edge(eng2, kg.make_edge_key('act.settle','depends_on','sys.ledger'), t2);

    PERFORM pg_temp.assert(
      EXISTS (SELECT 1 FROM kg.dependency_closure(eng2, 'act.settle', 4, t1)
               WHERE node_key = 'sys.ledger'),
      'as of commit 1, the closure must still see the path commit 2 retired -- '
      'this is what makes a pinned RL episode reproducible');
    PERFORM pg_temp.assert(
      NOT EXISTS (SELECT 1 FROM kg.dependency_closure(eng2, 'act.settle', 4)
                   WHERE node_key = 'sys.ledger'),
      'the 3-arg closure reads now(), where the dependency is gone');
    PERFORM pg_temp.assert(
      NOT EXISTS (SELECT 1 FROM kg.dependency_closure(eng2, 'act.settle', 4,
                                                      NULL::timestamptz)
                   WHERE node_key = 'sys.ledger'),
      'a NULL as-of must mean now(), matching kg.traverse');
    PERFORM pg_temp.assert(
      EXISTS (SELECT 1 FROM kg.impact_radius(eng2, 'sys.ledger', 4, t1)
               WHERE node_key = 'act.settle'),
      'impact_radius must honour as-of the same way');
    PERFORM pg_temp.assert(
      NOT EXISTS (SELECT 1 FROM kg.impact_radius(eng2, 'sys.ledger', 4)
                   WHERE node_key = 'act.settle'),
      'the 3-arg impact_radius reads now()');
  END;

  RAISE NOTICE '';
  RAISE NOTICE '================================';
  RAISE NOTICE '  ALL SMOKE TESTS PASSED';
  RAISE NOTICE '================================';
END $$;

ROLLBACK;
