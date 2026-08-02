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

  RAISE NOTICE '';
  RAISE NOTICE '================================';
  RAISE NOTICE '  ALL SMOKE TESTS PASSED';
  RAISE NOTICE '================================';
END $$;

ROLLBACK;
