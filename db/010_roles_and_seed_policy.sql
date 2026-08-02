-- =====================================================================
-- 010_roles_and_seed_policy.sql
-- Database roles (least privilege per component) + default gate policy.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Roles. Note what the agent role can NOT do: it has no write access to
-- kg.* at all. An agent that wants to change the graph must go through
-- hitl.proposal, and only the gate service can merge.
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fde_agent') THEN
    CREATE ROLE fde_agent NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fde_gate_service') THEN
    CREATE ROLE fde_gate_service NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fde_ingest') THEN
    CREATE ROLE fde_ingest NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fde_prodops') THEN
    CREATE ROLE fde_prodops NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fde_training') THEN
    CREATE ROLE fde_training NOLOGIN;
  END IF;
END $$;

-- Agent: read the graph, propose changes, write its own traces. Nothing else.
GRANT USAGE ON SCHEMA kg, hitl, wf, sor, trn TO fde_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA kg  TO fde_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA wf  TO fde_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA sor TO fde_agent;
GRANT SELECT, INSERT, UPDATE ON hitl.proposal, hitl.proposal_item TO fde_agent;
GRANT SELECT ON hitl.proposal_gate, hitl.gate_policy TO fde_agent;
GRANT INSERT, SELECT ON trn.trace_session, trn.trace_step, trn.failure_label TO fde_agent;
GRANT INSERT ON kg.source TO fde_agent;   -- may register evidence it captured
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA kg TO fde_agent;
GRANT EXECUTE ON FUNCTION hitl.submit_proposal(bigint) TO fde_agent;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA hitl, trn, kg TO fde_agent;
-- Explicitly denied:
REVOKE INSERT, UPDATE, DELETE ON kg.node, kg.edge, kg.commit FROM fde_agent;
REVOKE EXECUTE ON FUNCTION hitl.merge_proposal(bigint, text) FROM fde_agent;

-- Gate service: the only merger.
GRANT USAGE ON SCHEMA kg, hitl, wf, sor TO fde_gate_service;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA hitl TO fde_gate_service;
GRANT SELECT ON ALL TABLES IN SCHEMA kg TO fde_gate_service;
GRANT EXECUTE ON FUNCTION hitl.merge_proposal(bigint, text) TO fde_gate_service;
GRANT EXECUTE ON FUNCTION hitl.gates_satisfied(bigint) TO fde_gate_service;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA hitl TO fde_gate_service;

-- Ingest workers: SoR adapters + the embedder.
GRANT USAGE ON SCHEMA kg, sor TO fde_ingest;
GRANT SELECT, INSERT ON sor.observation TO fde_ingest;
GRANT SELECT, UPDATE ON sor.adapter TO fde_ingest;
GRANT SELECT, INSERT, UPDATE ON kg.node_embedding, kg.edge_embedding, kg.chunk, kg.embed_queue TO fde_ingest;
GRANT SELECT ON kg.node, kg.edge, kg.source TO fde_ingest;
GRANT EXECUTE ON FUNCTION sor.run_all_detectors(uuid) TO fde_ingest;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA kg, sor TO fde_ingest;

-- Product operations: run workflows, answer human steps, triage drift.
-- Read-only on the graph. Cannot publish a workflow (that is a gated action).
GRANT USAGE ON SCHEMA kg, wf, sor, hitl TO fde_prodops;
GRANT SELECT ON ALL TABLES IN SCHEMA kg, wf, sor TO fde_prodops;
GRANT SELECT, INSERT, UPDATE ON wf.run, wf.run_step TO fde_prodops;
GRANT SELECT, UPDATE ON sor.drift_signal TO fde_prodops;
GRANT SELECT, INSERT ON hitl.gate_decision TO fde_prodops;
GRANT SELECT ON hitl.proposal, hitl.proposal_item, hitl.proposal_gate TO fde_prodops;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA kg TO fde_prodops;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA wf, hitl TO fde_prodops;

-- Training pipeline: read traces and labels. No production write access.
GRANT USAGE ON SCHEMA trn, kg, hitl TO fde_training;
GRANT SELECT ON ALL TABLES IN SCHEMA trn TO fde_training;
GRANT SELECT ON ALL TABLES IN SCHEMA kg TO fde_training;
GRANT SELECT ON hitl.proposal, hitl.proposal_item, hitl.gate_decision TO fde_training;
GRANT INSERT, UPDATE ON trn.duel, trn.eval_query, trn.retriever_variant, trn.failure_label TO fde_training;
GRANT UPDATE (split, outcome, label_source) ON trn.trace_session TO fde_training;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA trn, kg TO fde_training;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA trn TO fde_training;

-- =====================================================================
-- DEFAULT GATE POLICY
--
-- These eight rules are the deterministic HITL spine. Tune the quorums and
-- SLAs per engagement, but do NOT remove rule 1 -- it is the catch-all that
-- makes hitl.submit_proposal's fail-closed check reachable rather than a
-- permanent exception.
-- =====================================================================

-- 1. CATCH-ALL. Every proposal gets at least one ontology review.
INSERT INTO hitl.gate_policy (engagement_id, name, gate_kind, match_op,
                              quorum, allow_self, sla_hours)
VALUES (NULL, 'catch-all ontology review', 'ontology', NULL, 1, false, 72);

-- 2. Any new PROCESS or CAPABILITY node is a structural claim about the
--    business. Two reviewers, because a wrong process boundary poisons every
--    workflow authored beneath it.
INSERT INTO hitl.gate_policy (engagement_id, name, gate_kind, match_op,
                              match_node_types, quorum, allow_self, sla_hours)
VALUES (NULL, 'new process/capability requires SME quorum', 'factual',
        ARRAY['add_node'], ARRAY['process','capability']::kg.node_type[], 2, false, 120);

-- 3 and 4. Anything touching a CONTROL goes to risk/compliance -- one rule for
--    the control NODE, one for the `gated_by` EDGE. No exceptions, no
--    self-approval, longer SLA because these reviewers are scarce.
--    (These are two separate policy rows; the numbering here matches the
--     policy_id each INSERT receives, so doc references stay accurate.)
INSERT INTO hitl.gate_policy (engagement_id, name, gate_kind,
                              match_node_types, quorum, allow_self, sla_hours)
VALUES (NULL, 'control changes require compliance sign-off', 'control',
        ARRAY['control']::kg.node_type[], 1, false, 168);

INSERT INTO hitl.gate_policy (engagement_id, name, gate_kind,
                              match_edge_types, quorum, allow_self, sla_hours)
VALUES (NULL, 'gated_by edges require compliance sign-off', 'control',
        ARRAY['gated_by']::kg.edge_type[], 1, false, 168);

-- 5. `automatable_by` is the edge that says "an agent may do this instead of
--    a person". It is the highest-consequence assertion in the ontology and
--    gets the process owner, not just an SME.
INSERT INTO hitl.gate_policy (engagement_id, name, gate_kind,
                              match_edge_types, quorum, allow_self, sla_hours)
VALUES (NULL, 'automation claims require process owner', 'automation',
        ARRAY['automatable_by']::kg.edge_type[], 1, false, 120);

-- 6. Low-evidence assertions get a factual gate even when nothing else fires.
--    Noisy-OR evidence strength below 0.65 means roughly "one mediocre source".
INSERT INTO hitl.gate_policy (engagement_id, name, gate_kind,
                              min_evidence_strength, quorum, allow_self, sla_hours)
VALUES (NULL, 'weak evidence requires SME confirmation', 'factual',
        0.65, 1, false, 72);

-- 7. RETIREMENT is destructive to downstream workflows. Always factual-gated.
INSERT INTO hitl.gate_policy (engagement_id, name, gate_kind, match_op,
                              quorum, allow_self, sla_hours)
VALUES (NULL, 'retirement requires confirmation', 'factual',
        ARRAY['retire_node','retire_edge'], 1, false, 72);

-- 8. Anything the agent itself flagged as touching customer or regulated data.
INSERT INTO hitl.gate_policy (engagement_id, name, gate_kind, match_jsonpath,
                              quorum, allow_self, sla_hours)
VALUES (NULL, 'regulated data touchpoints require compliance', 'control',
        '$.attributes.data_classification == "regulated"', 1, false, 168);

-- ---------------------------------------------------------------------
-- Baseline retriever variants for the rival grader tournament.
-- ---------------------------------------------------------------------
INSERT INTO trn.retriever_variant (name, description, config, is_champion) VALUES
 ('rrf-k60-2hop',
  'Production default: RRF k=60, 2-hop expansion, seed_k=30',
  '{"seed_k":30,"expand_hops":2,"rrf_k":60,"ef_search":100,"iterative_scan":"relaxed_order"}',
  true),
 ('ann-only',
  'Ablation: no graph expansion. Isolates how much the graph is actually adding.',
  '{"seed_k":30,"expand_hops":0,"rrf_k":60,"ef_search":100}', false),
 ('graph-heavy-3hop',
  'Wider expansion, smaller ANN seed. Tests recall-vs-precision at depth.',
  '{"seed_k":10,"expand_hops":3,"rrf_k":60,"ef_search":100}', false),
 ('high-recall-ef200',
  'ef_search at the top of the safe band. Tests whether ANN recall is the bottleneck.',
  '{"seed_k":50,"expand_hops":2,"rrf_k":60,"ef_search":200}', false);
