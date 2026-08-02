-- =====================================================================
-- 011_mcp_agent_supplemental_grants.sql
-- Minimal grant additions so the MCP server (mcp/server.py), which runs
-- every tool call as `fde_agent`, can perform the write-adjacent actions in
-- its documented tool surface. 010_roles_and_seed_policy.sql was written
-- for an agent that only reads the graph and proposes kg.* changes; the MCP
-- tool surface also includes drift triage and draft workflow authoring, both
-- of which are staging/annotation actions (never a publish, never a merge,
-- never a resolve) and need their own narrow grants.
--
-- Deliberately NOT granted, to preserve "agents propose, humans dispose":
--   * UPDATE on wf.workflow / wf.step        -- cannot flip status to
--                                                'published', cannot edit a
--                                                step after creation.
--   * Any privilege on hitl.merge_proposal    -- unchanged, still revoked
--                                                from PUBLIC in 005.
--   * Setting sor.drift_signal.state = 'resolved' -- not a grant boundary
--     (column-level GRANT can't restrict by value), enforced in
--     mcp/server.py's drift_triage tool instead.
-- =====================================================================

-- wf_draft: create a DRAFT workflow with its steps and faithfulness
-- bindings. INSERT-only -- once a row exists the agent cannot UPDATE it
-- (e.g. to publish), matching wf.assert_faithful being a pre-publish check
-- a human-operated path is expected to call before flipping status.
GRANT INSERT ON wf.workflow, wf.step, wf.step_binding TO fde_agent;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA wf TO fde_agent;

-- drift_triage: let the agent annotate a drift signal (state transitions
-- among the non-terminal states, a note, and who/when triaged it) without
-- being able to mark it resolved -- that check is enforced in application
-- code since GRANT has no per-value predicate.
GRANT UPDATE (state, triaged_by, triaged_at, resolution_note, raised_proposal_id)
  ON sor.drift_signal TO fde_agent;

-- kg_submit_proposal: hitl.submit_proposal(bigint) is already granted
-- EXECUTE to fde_agent in 010 (clearly intentional -- an agent submitting
-- its own proposal is the whole point of the HITL flow), but that function
-- is NOT SECURITY DEFINER: it runs with the CALLING role's own table
-- privileges, and 010 only gave fde_agent SELECT on hitl.proposal_gate. The
-- function itself DELETEs the previous gate set and INSERTs the freshly
-- computed one directly (verified empirically: submitting fails with
-- "permission denied for table proposal_gate" without this). No UPDATE is
-- needed -- nothing in the granted tool surface sets proposal_gate.cleared_at.
GRANT INSERT, DELETE ON hitl.proposal_gate TO fde_agent;

-- kg_proposal_status: reads "decisions so far" and "who is still needed",
-- which means SELECT on the decision log and on the reviewer roster/
-- authority tables. 010 never granted fde_agent any visibility into these
-- (its proposal-related grants stop at proposal/proposal_item/proposal_gate/
-- gate_policy) even though telling the agent who is still needed to clear a
-- gate is an explicitly required capability here, not a write.
GRANT SELECT ON hitl.gate_decision, hitl.reviewer, hitl.reviewer_authority TO fde_agent;

-- embedder_worker.py (runs as fde_ingest): verbalising an edge needs both
-- endpoints' CURRENT labels, read through kg.node_current. Views are not
-- covered by a GRANT on their underlying base table -- 010 granted
-- fde_ingest SELECT on kg.node/kg.edge (the base tables) but not on the
-- kg.node_current/kg.edge_current views, and Postgres checks view
-- privileges separately from the tables a view selects from. Verified
-- empirically: the worker fails with "permission denied for view
-- node_current" without this.
GRANT SELECT ON kg.node_current, kg.edge_current TO fde_ingest;

-- tracing.end_session (agents/common/tracing.py): the agent opens its own
-- trace session at start and closes it at the end, writing ended_at,
-- final_output, total_tokens and latency_ms. 010 granted fde_agent only
-- INSERT/SELECT on trn.trace_session, so the closing UPDATE could never
-- succeed -- every session would sit permanently open with a NULL
-- final_output, which quietly breaks the SFT export (trn.sft_export filters
-- on outcome, and outcome is set from these closing writes).
--
-- Column-scoped on purpose. `outcome`, `label_proposal_id`, `label_source`
-- and `split` are deliberately excluded: those are the TRAINING LABEL, and
-- an agent that can write its own label can mark its own work accepted.
-- Only the gate service (via the HITL merge path) and fde_training may set
-- them.
GRANT UPDATE (ended_at, final_output, total_tokens, latency_ms)
  ON trn.trace_session TO fde_agent;

-- The grounding checker and eval harness annotate steps after the fact.
GRANT UPDATE (grounded, grounding_detail) ON trn.trace_step TO fde_training;

-- The gate service writes the label when a proposal resolves. This is the
-- single most important privilege boundary in the training pipeline: the
-- label comes from the human gate, never from the agent being trained.
GRANT UPDATE (outcome, label_proposal_id, label_source)
  ON trn.trace_session TO fde_gate_service;
GRANT SELECT ON ALL TABLES IN SCHEMA trn TO fde_gate_service;

-- drift_scan (mcp/server.py): the Workflow Agent's autonomous monitoring loop
-- calls sor.run_all_detectors. 010 granted EXECUTE only to fde_ingest, so the
-- tool was structurally unreachable for the role every MCP call runs as --
-- it failed with "permission denied for function run_all_detectors" and was
-- swallowed by the tool's generic error boundary, which is exactly how a
-- monitoring loop ends up silently monitoring nothing.
--
-- Safe to grant: the function is SECURITY DEFINER and only writes to
-- sor.drift_signal. It cannot touch kg.* or hitl.*.
GRANT EXECUTE ON FUNCTION sor.run_all_detectors(uuid) TO fde_agent;
GRANT SELECT ON sor.observed_transition, sor.observation, sor.adapter TO fde_agent;
