-- =====================================================================
-- 012_rl_rollout_role.sql
-- Least-privilege role for RL rollout workers.
--
-- An RL rollout runs the SAME retrieval environment as production, N
-- episodes concurrently, driven by an untrusted policy under training. That
-- policy will, by construction, emit malformed and adversarial tool calls --
-- that is what exploration means. So the rollout role gets strictly less
-- than fde_agent: it can read the graph and write its own traces, and it has
-- no path to hitl.* at all. A rollout cannot create a proposal, cannot
-- submit one, and therefore cannot manufacture its own training label.
--
-- This mirrors the rule that makes the whole platform trainable: the label
-- comes from the human gate, never from the thing being trained.
-- =====================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fde_rl_rollout') THEN
    CREATE ROLE fde_rl_rollout NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA kg, trn TO fde_rl_rollout;

-- Read the graph. Same retrieval surface production serves, so the
-- environment the policy is optimised against is the environment it will be
-- deployed into. Any divergence here silently trains the wrong policy.
GRANT SELECT ON ALL TABLES IN SCHEMA kg TO fde_rl_rollout;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA kg TO fde_rl_rollout;

-- Write its own trajectory. DELETE is granted so a discarded rollout can
-- clean up after itself rather than leaving orphan sessions that pollute
-- the SFT export.
GRANT SELECT, INSERT, DELETE ON trn.trace_session, trn.trace_step TO fde_rl_rollout;
GRANT SELECT ON trn.eval_query, trn.retriever_variant TO fde_rl_rollout;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA trn TO fde_rl_rollout;

-- Closing out an episode. Exactly the column set fde_agent gets in db/011,
-- and for the same reason: a worker may record how its own run went, but not
-- what that run is WORTH as training data.
--
-- `outcome` is deliberately absent. It is the column trn.sft_export filters
-- on (outcome IN ('accepted','corrected')), so a rollout that could set it
-- could inject its own exploration -- including deliberately malformed
-- exploratory tool calls -- into the supervised training set as though a
-- human had approved it. Rollout rows therefore stay at the 'pending'
-- default forever and fall out of sft_export by construction, with no
-- filter for anyone to forget. Episode terminal state goes in final_output.
GRANT UPDATE (ended_at, final_output, total_tokens, latency_ms)
  ON trn.trace_session TO fde_rl_rollout;

-- Explicitly denied. Listed rather than merely omitted so a future reviewer
-- reading this file sees the intent, not just the absence.
REVOKE ALL ON SCHEMA hitl FROM fde_rl_rollout;
REVOKE ALL ON SCHEMA wf   FROM fde_rl_rollout;
REVOKE ALL ON SCHEMA sor  FROM fde_rl_rollout;
-- Cannot label its own work -- the label columns specifically:
REVOKE UPDATE (outcome, label_proposal_id, label_source, split)
  ON trn.trace_session FROM fde_rl_rollout;

COMMENT ON ROLE fde_rl_rollout IS
  'RL rollout worker. Reads kg.*, writes its own trn traces, has no access '
  'to hitl/wf/sor and cannot write trn.trace_session.outcome, so its episodes '
  'never enter trn.sft_export.';
