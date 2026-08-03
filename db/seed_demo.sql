-- =====================================================================
-- seed_demo.sql
-- DEMO DATA. Local evaluation databases only -- never production.
--
-- db/tests/smoke_test.sql proves the HITL spine works, but it ends in
-- ROLLBACK, so a freshly rebuilt database has an empty review console and
-- nothing for a first-time reader to look at. This file leaves one
-- engagement in a realistic mid-review state and COMMITs it:
--
--   * one MERGED proposal  -> a sealed kg.commit carrying 6 nodes, 6 edges
--   * three PENDING proposals in the review queue -- one submitted, one
--     in_review with a gate already cleared, one changes_requested --
--     every item citing the kg.source rows it was extracted from
--   * three reviewers holding exactly the authorities those gates need
--
-- Apply it with:  ./db/rebuild.sh <db> --with-demo
-- Then serve the console against it:
--   FDE_GATE_DEV_PRINCIPAL=sme@example.com uv run fde-gate-dev
--
-- The privilege discipline IS the product, so the seed obeys it rather
-- than shortcutting it: proposals are authored as `fde_agent`, and every
-- review decision and the merge run as `fde_gate_service` through
-- hitl.record_decision / hitl.merge_proposal. Only the bootstrap identities
-- -- genesis commit, sources, reviewer roster -- are written as the
-- migration owner, which is how a real engagement is set up. No grants are
-- added, nothing is SECURITY DEFINER, and nothing writes kg.node/kg.edge
-- outside the merge function.
-- =====================================================================
\set ON_ERROR_STOP on
\set QUIET on
\pset pager off
SET client_min_messages = notice;

BEGIN;

DO $$
DECLARE
  eng             uuid := '11111111-1111-1111-1111-111111111111';
  genesis         bigint;
  src_revops      bigint; src_sop bigint; src_ar bigint;
  rev_sme         bigint; rev_comp bigint; rev_owner bigint;
  prop_mapping    bigint; prop_billing bigint;
  prop_automation bigint; prop_threshold bigint;
  gate            record;
  c               kg.commit;
  g_id            bigint;
  n               int;
BEGIN
  -- Re-running this file over its own output collides on the reviewer
  -- roster with a bare unique violation. Say why instead.
  IF EXISTS (SELECT 1 FROM hitl.proposal WHERE engagement_id = eng) THEN
    RAISE EXCEPTION 'demo data is already present for engagement %; seed_demo.sql '
                    'is not idempotent -- rebuild the database instead '
                    '(db/rebuild.sh <db> --with-demo)', eng;
  END IF;

  ------------------------------------------------------------------
  RAISE NOTICE '--- bootstrap: genesis commit, sources, reviewer roster';
  ------------------------------------------------------------------
  INSERT INTO kg.commit (status, engagement_id, title, authored_by, sealed_by, sealed_at)
  VALUES ('sealed', eng, 'genesis', 'bootstrap', 'bootstrap', now())
  RETURNING commit_id INTO genesis;

  INSERT INTO kg.source (engagement_id, source_kind, title, captured_at, captured_by)
  VALUES (eng, 'interview', 'RevOps lead interview 2026-07-14', now() - interval '3 days', 'fde:kh')
  RETURNING source_id INTO src_revops;

  INSERT INTO kg.source (engagement_id, source_kind, title, captured_at, captured_by)
  VALUES (eng, 'sop_document', 'Discount Approval SOP v4', now() - interval '10 days', 'fde:kh')
  RETURNING source_id INTO src_sop;

  INSERT INTO kg.source (engagement_id, source_kind, title, captured_at, captured_by)
  VALUES (eng, 'interview', 'AR and collections interview 2026-07-21',
          now() - interval '1 day', 'fde:kh')
  RETURNING source_id INTO src_ar;

  INSERT INTO hitl.reviewer (principal, display_name) VALUES
    ('sme@example.com','RevOps SME')        RETURNING reviewer_id INTO rev_sme;
  INSERT INTO hitl.reviewer (principal, display_name) VALUES
    ('compliance@example.com','Compliance') RETURNING reviewer_id INTO rev_comp;
  INSERT INTO hitl.reviewer (principal, display_name) VALUES
    ('owner@example.com','Process Owner')   RETURNING reviewer_id INTO rev_owner;

  INSERT INTO hitl.reviewer_authority (reviewer_id, engagement_id, gate_kind, granted_by) VALUES
    (rev_sme,   eng, 'ontology',   'bootstrap'),
    (rev_sme,   eng, 'factual',    'bootstrap'),
    (rev_comp,  eng, 'ontology',   'bootstrap'),
    (rev_comp,  eng, 'control',    'bootstrap'),
    (rev_owner, eng, 'factual',    'bootstrap'),
    (rev_owner, eng, 'automation', 'bootstrap');

  -- The first administrator (db/016). Written here as the owner, by hand,
  -- because that is the only way a first admin can ever exist: /ui/reviewers
  -- requires an admin to grant admin. A real deployment does this once,
  -- against its own principal, and then never touches SQL again -- docs/10
  -- carries the statement.
  INSERT INTO hitl.reviewer_admin (reviewer_id, granted_by) VALUES
    (rev_owner, 'bootstrap');

  ------------------------------------------------------------------
  RAISE NOTICE '--- fde_agent: the Engagement Agent proposes the Q2C mapping';
  ------------------------------------------------------------------
  EXECUTE 'SET LOCAL ROLE fde_agent';

  INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                             agent_name, base_commit_id, model_id)
  VALUES (eng, 'Q2C discount approval — initial mapping',
          'Derived from the RevOps interview and SOP v4. Two independent sources '
          'agree on the >20% escalation threshold.',
          'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-engagement',
          'engagement', genesis, 'claude-sonnet-4')
  RETURNING proposal_id INTO prop_mapping;

  INSERT INTO hitl.proposal_item
    (proposal_id, ordinal, op, node_type, subject_key, payload, source_ids, agent_confidence)
  VALUES
   (prop_mapping, 1, 'add_node', 'process', 'proc.quote_to_cash',
    '{"label":"Quote to Cash","summary":"End-to-end from quote creation to booked revenue.","attributes":{}}',
    ARRAY[src_revops, src_sop], 0.92),
   (prop_mapping, 2, 'add_node', 'activity', 'act.create_quote',
    '{"label":"Create Quote","summary":"Sales rep builds a quote in CPQ.","attributes":{}}',
    ARRAY[src_revops], 0.95),
   (prop_mapping, 3, 'add_node', 'activity', 'act.discount_review',
    '{"label":"Discount Review","summary":"Deal desk reviews quotes discounted beyond policy.","attributes":{}}',
    ARRAY[src_revops, src_sop], 0.90),
   (prop_mapping, 4, 'add_node', 'control', 'ctl.discount_threshold_20',
    '{"label":"20% Discount Threshold","summary":"Quotes above 20% discount require deal desk approval before send.","attributes":{"data_classification":"internal"}}',
    ARRAY[src_sop], 0.88),
   (prop_mapping, 5, 'add_node', 'system', 'sys.cpq',
    '{"label":"CPQ","summary":"Configure-price-quote system of record for quotes.","attributes":{}}',
    ARRAY[src_revops], 0.97),
   (prop_mapping, 6, 'add_node', 'role', 'role.deal_desk',
    '{"label":"Deal Desk Analyst","summary":"Reviews and approves non-standard pricing.","attributes":{"is_role_title":true}}',
    ARRAY[src_revops], 0.94);

  INSERT INTO hitl.proposal_item
    (proposal_id, ordinal, op, edge_type, subject_key, payload, source_ids, agent_confidence)
  VALUES
   (prop_mapping, 7, 'add_edge', 'belongs_to',
    kg.make_edge_key('act.create_quote','belongs_to','proc.quote_to_cash'),
    '{"src_key":"act.create_quote","dst_key":"proc.quote_to_cash"}', ARRAY[src_revops], 0.95),
   (prop_mapping, 8, 'add_edge', 'belongs_to',
    kg.make_edge_key('act.discount_review','belongs_to','proc.quote_to_cash'),
    '{"src_key":"act.discount_review","dst_key":"proc.quote_to_cash"}', ARRAY[src_revops], 0.95),
   (prop_mapping, 9, 'add_edge', 'precedes',
    kg.make_edge_key('act.create_quote','precedes','act.discount_review'),
    '{"src_key":"act.create_quote","dst_key":"act.discount_review","attributes":{"sla_seconds":14400}}',
    ARRAY[src_revops, src_sop], 0.91),
   (prop_mapping, 10, 'add_edge', 'gated_by',
    kg.make_edge_key('act.create_quote','gated_by','ctl.discount_threshold_20'),
    '{"src_key":"act.create_quote","dst_key":"ctl.discount_threshold_20"}', ARRAY[src_sop], 0.89),
   (prop_mapping, 11, 'add_edge', 'depends_on',
    kg.make_edge_key('act.discount_review','depends_on','sys.cpq'),
    '{"src_key":"act.discount_review","dst_key":"sys.cpq"}', ARRAY[src_revops], 0.93),
   (prop_mapping, 12, 'add_edge', 'performs',
    kg.make_edge_key('role.deal_desk','performs','act.discount_review'),
    '{"src_key":"role.deal_desk","dst_key":"act.discount_review"}', ARRAY[src_revops], 0.96);

  PERFORM hitl.submit_proposal(prop_mapping);

  EXECUTE 'RESET ROLE';

  ------------------------------------------------------------------
  RAISE NOTICE '--- fde_gate_service: reviewers clear every gate, then merge';
  ------------------------------------------------------------------
  EXECUTE 'SET LOCAL ROLE fde_gate_service';

  FOR gate IN SELECT * FROM hitl.proposal_gate
               WHERE proposal_id = prop_mapping ORDER BY gate_id LOOP
    PERFORM hitl.record_decision(gate.gate_id,
      CASE gate.gate_kind WHEN 'control'    THEN 'compliance@example.com'
                          WHEN 'automation' THEN 'owner@example.com'
                          ELSE 'sme@example.com' END,
      'approve',
      CASE gate.gate_kind
        WHEN 'control' THEN 'Threshold matches SOP v4 section 3. Approved.'
        WHEN 'factual' THEN 'Matches the transcript; the deal desk hop is real.'
        ELSE 'Node and edge types are the right ones for this shape.' END,
      '{}'::jsonb,
      CASE gate.gate_kind WHEN 'control' THEN 600 WHEN 'factual' THEN 420 ELSE 240 END);

    IF gate.quorum > 1 THEN
      PERFORM hitl.record_decision(gate.gate_id, 'owner@example.com', 'approve',
        'Second reviewer: the process boundary is where I would draw it too.',
        '{}'::jsonb, 380);
    END IF;
  END LOOP;

  SELECT * INTO c FROM hitl.merge_proposal(prop_mapping, 'owner@example.com');

  EXECUTE 'RESET ROLE';

  ------------------------------------------------------------------
  RAISE NOTICE '--- fde_agent: three follow-on proposals off the merged commit';
  ------------------------------------------------------------------
  EXECUTE 'SET LOCAL ROLE fde_agent';

  -- 1. Untouched in the queue: nobody has opened it yet.
  INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                             agent_name, base_commit_id, model_id)
  VALUES (eng, 'Extend Q2C downstream into billing and collections',
          'The AR interview describes two activities after the quote is booked '
          'that the initial mapping stops short of.',
          'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-engagement',
          'engagement', c.commit_id, 'claude-sonnet-4')
  RETURNING proposal_id INTO prop_billing;

  INSERT INTO hitl.proposal_item
    (proposal_id, ordinal, op, node_type, subject_key, payload, source_ids, agent_confidence)
  VALUES
   (prop_billing, 1, 'add_node', 'activity', 'act.apply_payment',
    '{"label":"Apply Payment","summary":"AR applies a received payment against the open invoice.","attributes":{}}',
    ARRAY[src_ar], 0.81),
   (prop_billing, 2, 'add_node', 'activity', 'act.dunning',
    '{"label":"Dunning","summary":"Collections chases invoices past their payment SLA.","attributes":{}}',
    ARRAY[src_ar], 0.74);

  INSERT INTO hitl.proposal_item
    (proposal_id, ordinal, op, edge_type, subject_key, payload, source_ids, agent_confidence)
  VALUES
   (prop_billing, 3, 'add_edge', 'belongs_to',
    kg.make_edge_key('act.apply_payment','belongs_to','proc.quote_to_cash'),
    '{"src_key":"act.apply_payment","dst_key":"proc.quote_to_cash"}', ARRAY[src_ar], 0.83),
   (prop_billing, 4, 'add_edge', 'belongs_to',
    kg.make_edge_key('act.dunning','belongs_to','proc.quote_to_cash'),
    '{"src_key":"act.dunning","dst_key":"proc.quote_to_cash"}', ARRAY[src_ar], 0.77);

  PERFORM hitl.submit_proposal(prop_billing);

  -- 2. Part-reviewed: the automation claim is the one a person must weigh.
  INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                             agent_name, base_commit_id, model_id)
  VALUES (eng, 'Automate the discount check with a CPQ tool binding',
          'The discount lookup deal desk performs by hand is a single read-only '
          'CPQ call, so the review step could be agent-assisted.',
          'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-workflow',
          'workflow', c.commit_id, 'claude-sonnet-4')
  RETURNING proposal_id INTO prop_automation;

  INSERT INTO hitl.proposal_item
    (proposal_id, ordinal, op, node_type, subject_key, payload, source_ids, agent_confidence)
  VALUES
   (prop_automation, 1, 'add_node', 'tool_binding', 'tool.cpq_discount_check',
    '{"label":"CPQ Discount Check","summary":"Read-only CPQ call returning a quote discount percentage and its deal desk approval state.","attributes":{"transport":"rest","idempotent":true}}',
    ARRAY[src_revops, src_sop], 0.86);

  INSERT INTO hitl.proposal_item
    (proposal_id, ordinal, op, edge_type, subject_key, payload, source_ids, agent_confidence)
  VALUES
   (prop_automation, 2, 'add_edge', 'automatable_by',
    kg.make_edge_key('act.discount_review','automatable_by','tool.cpq_discount_check'),
    '{"src_key":"act.discount_review","dst_key":"tool.cpq_discount_check"}',
    ARRAY[src_revops], 0.71);

  PERFORM hitl.submit_proposal(prop_automation);

  -- 3. Pushed back: a control change with no updated SOP behind it.
  INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                             agent_name, base_commit_id, model_id)
  VALUES (eng, 'Raise the discount approval threshold to 25 percent',
          'The RevOps lead said in passing that the deal desk now waves through '
          'anything under 25%.',
          'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-engagement',
          'engagement', c.commit_id, 'claude-sonnet-4')
  RETURNING proposal_id INTO prop_threshold;

  INSERT INTO hitl.proposal_item
    (proposal_id, ordinal, op, node_type, subject_key, payload, source_ids, agent_confidence)
  VALUES
   (prop_threshold, 1, 'update_node', 'control', 'ctl.discount_threshold_20',
    '{"label":"20% Discount Threshold","summary":"Quotes above 25% discount require deal desk approval before send.","attributes":{"data_classification":"internal"}}',
    ARRAY[src_revops], 0.66);

  PERFORM hitl.submit_proposal(prop_threshold);

  EXECUTE 'RESET ROLE';

  ------------------------------------------------------------------
  RAISE NOTICE '--- fde_gate_service: partial review, so the queue is mid-flight';
  ------------------------------------------------------------------
  EXECUTE 'SET LOCAL ROLE fde_gate_service';

  -- The SME clears the ontology gate and deliberately leaves the automation
  -- claim to the process owner: submitted -> in_review, one gate cleared.
  SELECT gate_id INTO g_id FROM hitl.proposal_gate
   WHERE proposal_id = prop_automation AND gate_kind = 'ontology' LIMIT 1;
  PERFORM hitl.record_decision(g_id, 'sme@example.com', 'approve',
    'Tool binding is named and typed correctly. Whether the step may be '
    'automated at all is the process owner''s call, not mine.',
    '{}'::jsonb, 180);

  -- Compliance sends the threshold change back: -> changes_requested.
  SELECT gate_id INTO g_id FROM hitl.proposal_gate
   WHERE proposal_id = prop_threshold AND gate_kind = 'control' LIMIT 1;
  PERFORM hitl.record_decision(g_id, 'compliance@example.com', 'request_changes',
    'SOP v4 still says 20%. Attach the signed policy change and resubmit -- '
    'a remark in an interview is not a control change.',
    '{}'::jsonb, 300);

  EXECUTE 'RESET ROLE';

  ------------------------------------------------------------------
  RAISE NOTICE '--- checks: the seed produced what it promises';
  ------------------------------------------------------------------
  SELECT count(*) INTO n FROM hitl.proposal
   WHERE engagement_id = eng AND status IN ('submitted','in_review','changes_requested');
  IF n <> 3 THEN
    RAISE EXCEPTION 'expected 3 pending proposals in the review queue, got %', n;
  END IF;

  IF (SELECT count(DISTINCT status) FROM hitl.proposal
       WHERE engagement_id = eng AND status IN ('submitted','in_review','changes_requested')) <> 3
  THEN
    RAISE EXCEPTION 'the three pending proposals must sit in three different states';
  END IF;

  SELECT count(*) INTO n FROM hitl.proposal
   WHERE engagement_id = eng AND status = 'merged' AND merged_commit_id IS NOT NULL;
  IF n <> 1 THEN
    RAISE EXCEPTION 'expected exactly 1 merged proposal, got %', n;
  END IF;

  SELECT count(*) INTO n FROM kg.node_current WHERE engagement_id = eng;
  IF n <> 6 THEN
    RAISE EXCEPTION 'expected 6 live nodes from the merged commit, got %', n;
  END IF;

  SELECT count(*) INTO n FROM kg.edge_current WHERE engagement_id = eng;
  IF n <> 6 THEN
    RAISE EXCEPTION 'expected 6 live edges from the merged commit, got %', n;
  END IF;

  IF EXISTS (SELECT 1 FROM hitl.proposal_item i
              JOIN hitl.proposal p USING (proposal_id)
             WHERE p.engagement_id = eng AND cardinality(i.source_ids) = 0) THEN
    RAISE EXCEPTION 'every demo proposal item must cite at least one kg.source';
  END IF;

  -- Exactly one admin, and it is the principal the console instructions
  -- name. A demo whose /ui/reviewers page 403s the account the README tells
  -- the reader to sign in as would look like a broken feature.
  IF NOT hitl.is_reviewer_admin('owner@example.com') THEN
    RAISE EXCEPTION 'owner@example.com must hold the admin authority, or the '
                    'demo console shows no Reviewers page';
  END IF;

  SELECT count(*) INTO n FROM hitl.reviewer_admin WHERE revoked_at IS NULL;
  IF n <> 1 THEN
    RAISE EXCEPTION 'expected exactly 1 live admin in the demo seed, got %', n;
  END IF;

  RAISE NOTICE '';
  RAISE NOTICE '================================';
  RAISE NOTICE '  DEMO DATA SEEDED';
  RAISE NOTICE '    engagement   %', eng;
  RAISE NOTICE '    merged commit %', c.commit_id;
  RAISE NOTICE '    review queue  3 proposals (submitted, in_review, changes_requested)';
  RAISE NOTICE '    reviewers     sme@ / compliance@ / owner@example.com';
  RAISE NOTICE '    admin         owner@example.com (sign in as them for /ui/reviewers)';
  RAISE NOTICE '================================';
END $$;

COMMIT;
