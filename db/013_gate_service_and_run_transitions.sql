-- =====================================================================
-- 013_gate_service_and_run_transitions.sql
-- The review decisions and run transitions the gate service performs.
--
-- 004 gave us the deterministic gate COMPUTATION and 005 the merge. What was
-- missing is everything between: there was no hitl.approve/hitl.reject, so
-- "approving" meant hand-writing an INSERT into hitl.gate_decision plus an
-- UPDATE of hitl.proposal.status, and no role could publish a workflow or
-- move a run at all. This file puts those transitions in SQL for the same
-- reason 004 puts gate computation in SQL: a transition that lives in
-- application code is a transition each new caller can get subtly wrong,
-- and these are exactly the transitions that decide what the training
-- pipeline believes a human approved.
--
-- No new tables. The 006/004 schema already models every state; only the
-- functions were missing. That matters for grants: 010's `ALL TABLES`
-- grants are point-in-time, so a new table here would silently arrive
-- ungranted. New FUNCTIONS have the opposite footgun -- they arrive
-- PUBLIC-executable -- which is why every one of them is revoked below.
--
-- Three defects this file closes, named so a future reader can find them:
--   B17 -- 010 granted fde_gate_service USAGE on kg/hitl/wf/sor but not trn,
--          while 011 granted it trn TABLE privileges. The label write was
--          unreachable at runtime. Fixed in the grants section.
--   B18 -- no role holds UPDATE on wf.workflow (that is a CI invariant), so
--          publishing was impossible for everyone. wf.publish_workflow is
--          SECURITY DEFINER, exactly as hitl.merge_proposal is, and is the
--          only publish path.
--   B19 -- hitl.gate_decision's partial unique index plus its non-deferrable
--          self-FK force a three-step supersede ordering. See
--          hitl.record_decision.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Per-gate quorum predicate.
--
-- hitl.gates_satisfied (004) answers "is the whole proposal clear?" by
-- NOT EXISTS over every gate. Recording a decision needs the same predicate
-- for ONE gate, so it can stamp proposal_gate.cleared_at as each gate is
-- met. The body below is the gates_satisfied predicate for a single gate,
-- inverted -- deliberately duplicated rather than refactored, so that
-- changing the quorum rule in one place cannot silently diverge the other:
-- both bodies are in this repo and any edit to one is visibly an edit to a
-- pair.
-- ---------------------------------------------------------------------
CREATE FUNCTION hitl.gate_quorum_met(p_gate_id bigint)
RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT (SELECT count(DISTINCT d.reviewer_id)
            FROM hitl.gate_decision d
            JOIN hitl.reviewer r ON r.reviewer_id = d.reviewer_id
            JOIN hitl.reviewer_authority ra
              ON ra.reviewer_id   = d.reviewer_id
             AND ra.gate_kind     = g.gate_kind
             AND ra.revoked_at   IS NULL
             AND ra.engagement_id = p.engagement_id
           WHERE d.gate_id = g.gate_id
             AND d.decision = 'approve'
             AND d.superseded_by IS NULL
             AND r.is_active
             AND (g.allow_self OR r.principal <> p.authored_by)
         ) >= g.quorum
     AND NOT EXISTS (
           SELECT 1 FROM hitl.gate_decision d
            WHERE d.gate_id = g.gate_id
              AND d.superseded_by IS NULL
              AND d.decision IN ('reject','request_changes'))
    FROM hitl.proposal_gate g
    JOIN hitl.proposal p ON p.proposal_id = g.proposal_id
   WHERE g.gate_id = p_gate_id;
$$;

COMMENT ON FUNCTION hitl.gate_quorum_met IS
  'Single-gate form of hitl.gates_satisfied''s predicate. Used by '
  'hitl.record_decision to stamp proposal_gate.cleared_at gate by gate.';

-- ---------------------------------------------------------------------
-- hitl.record_decision -- the review action.
--
-- Fail-closed and loud. Every reason a decision would not have counted
-- (unknown principal, inactive reviewer, no authority for this gate kind,
-- self-review on a gate that forbids it, proposal not open) raises instead
-- of being silently dropped from the quorum count. 004's gates_satisfied
-- has to be quiet about these -- it is a predicate -- so this is the layer
-- where a reviewer finds out their click did nothing.
-- ---------------------------------------------------------------------
CREATE FUNCTION hitl.record_decision(
  p_gate_id        bigint,
  p_principal      text,
  p_decision       hitl.decision,
  p_comment        text  DEFAULT NULL,
  p_item_verdicts  jsonb DEFAULT '{}'::jsonb,
  p_review_seconds int   DEFAULT NULL
)
RETURNS hitl.gate_decision
LANGUAGE plpgsql AS $$
DECLARE
  g         hitl.proposal_gate;
  p         hitl.proposal;
  r         hitl.reviewer;
  d         hitl.gate_decision;
  v_prev    bigint;
  v_verdicts jsonb := coalesce(p_item_verdicts, '{}'::jsonb);
  v_key     text;
  v_val     jsonb;
  v_verdict text;
  v_item_id bigint;
  v_gate    record;
BEGIN
  SELECT * INTO g FROM hitl.proposal_gate WHERE gate_id = p_gate_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'gate % not found', p_gate_id;
  END IF;

  SELECT * INTO p FROM hitl.proposal WHERE proposal_id = g.proposal_id FOR UPDATE;
  IF p.status NOT IN ('submitted','in_review') THEN
    RAISE EXCEPTION 'proposal % is %, not open for review', p.proposal_id, p.status;
  END IF;

  SELECT * INTO r FROM hitl.reviewer WHERE principal = p_principal;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'principal % is not a registered reviewer', p_principal;
  END IF;
  IF NOT r.is_active THEN
    RAISE EXCEPTION 'reviewer % is not active', p_principal;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM hitl.reviewer_authority ra
                  WHERE ra.reviewer_id   = r.reviewer_id
                    AND ra.engagement_id = p.engagement_id
                    AND ra.gate_kind     = g.gate_kind
                    AND ra.revoked_at   IS NULL) THEN
    RAISE EXCEPTION 'reviewer % holds no live % authority on engagement % '
                    '(grant it in hitl.reviewer_authority; a decision recorded '
                    'without authority would never count toward quorum)',
                    p_principal, g.gate_kind, p.engagement_id;
  END IF;

  IF NOT g.allow_self AND r.principal = p.authored_by THEN
    RAISE EXCEPTION 'reviewer % authored proposal %; this gate does not allow '
                    'self-review', p_principal, p.proposal_id;
  END IF;

  -- ------------------------------------------------------------------
  -- B19. A reviewer changing their mind must end up with exactly one live
  -- row, and gate_decision_live_uq is UNIQUE (gate_id, reviewer_id) WHERE
  -- superseded_by IS NULL while superseded_by is a non-deferrable FK back
  -- into this same table. That leaves exactly one legal ordering:
  --   1. point the old row at ITSELF -- it leaves the partial index (the
  --      index only covers NULLs) and the FK is satisfied by a row that
  --      already exists;
  --   2. insert the new decision, now unopposed in the partial index;
  --   3. repoint the old row at the new decision_id.
  -- Inserting first violates the index; nulling first is what the index
  -- already has; setting superseded_by to a not-yet-existing id violates
  -- the FK. Any "obvious" ordering fails.
  -- ------------------------------------------------------------------
  SELECT decision_id INTO v_prev
    FROM hitl.gate_decision
   WHERE gate_id = p_gate_id AND reviewer_id = r.reviewer_id AND superseded_by IS NULL
   FOR UPDATE;

  IF v_prev IS NOT NULL THEN
    UPDATE hitl.gate_decision SET superseded_by = decision_id WHERE decision_id = v_prev;
  END IF;

  INSERT INTO hitl.gate_decision
    (gate_id, reviewer_id, decision, comment, item_verdicts, review_seconds)
  VALUES (p_gate_id, r.reviewer_id, p_decision, p_comment, v_verdicts, p_review_seconds)
  RETURNING * INTO d;

  IF v_prev IS NOT NULL THEN
    UPDATE hitl.gate_decision SET superseded_by = d.decision_id WHERE decision_id = v_prev;
  END IF;

  -- ------------------------------------------------------------------
  -- Per-item verdicts. The decision row keeps them verbatim as the audit
  -- and training record; proposal_item carries the effect, because that is
  -- what hitl.merge_proposal reads.
  -- ------------------------------------------------------------------
  FOR v_key, v_val IN SELECT * FROM jsonb_each(v_verdicts) LOOP
    v_verdict := v_val->>'verdict';
    IF v_verdict IS NULL OR v_verdict NOT IN ('accept','drop','edit') THEN
      RAISE EXCEPTION 'item % has verdict %; expected accept, drop or edit',
                      v_key, coalesce(v_verdict, '<missing>');
    END IF;

    SELECT item_id INTO v_item_id FROM hitl.proposal_item
     WHERE item_id = v_key::bigint AND proposal_id = p.proposal_id;
    IF v_item_id IS NULL THEN
      RAISE EXCEPTION 'item % is not an item of proposal %', v_key, p.proposal_id;
    END IF;

    IF v_verdict = 'accept' THEN
      UPDATE hitl.proposal_item SET item_status = 'accepted' WHERE item_id = v_item_id;

    ELSIF v_verdict = 'drop' THEN
      UPDATE hitl.proposal_item SET item_status = 'dropped' WHERE item_id = v_item_id;

    ELSE
      IF jsonb_typeof(v_val->'payload') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'item % has an edit verdict with no object "payload"', v_key;
      END IF;
      UPDATE hitl.proposal_item
         SET original_payload = coalesce(original_payload, payload),
             payload          = v_val->'payload',
             edited_by        = p_principal,
             edited_at        = now(),
             item_status      = 'edited'
       WHERE item_id = v_item_id;
    END IF;
  END LOOP;

  -- ------------------------------------------------------------------
  -- Status machine.
  -- ------------------------------------------------------------------
  IF p_decision = 'reject' THEN
    UPDATE hitl.proposal
       SET status = 'rejected', decided_at = now(), updated_at = now()
     WHERE proposal_id = p.proposal_id;
    -- A rejection is a terminal human judgment, so it labels the trace now.
    PERFORM hitl.apply_trace_label(p.proposal_id);

  ELSIF p_decision = 'request_changes' THEN
    UPDATE hitl.proposal
       SET status = 'changes_requested', updated_at = now()
     WHERE proposal_id = p.proposal_id;

  ELSE
    -- approve / abstain. An abstention is recorded (it is evidence about
    -- the reviewer roster) but contributes nothing to any quorum.
    IF p.status = 'submitted' THEN
      UPDATE hitl.proposal SET status = 'in_review', updated_at = now()
       WHERE proposal_id = p.proposal_id;
    END IF;

    FOR v_gate IN SELECT gate_id FROM hitl.proposal_gate
                   WHERE proposal_id = p.proposal_id AND cleared_at IS NULL LOOP
      IF hitl.gate_quorum_met(v_gate.gate_id) THEN
        UPDATE hitl.proposal_gate SET cleared_at = now() WHERE gate_id = v_gate.gate_id;
      END IF;
    END LOOP;

    IF hitl.gates_satisfied(p.proposal_id) THEN
      UPDATE hitl.proposal
         SET status = 'approved', decided_at = now(), updated_at = now()
       WHERE proposal_id = p.proposal_id;
    END IF;
  END IF;

  RETURN d;
END $$;

COMMENT ON FUNCTION hitl.record_decision IS
  'The review action. Fail-closed on authority, self-review and proposal '
  'state; supersedes the reviewer''s previous live decision in the only '
  'ordering the partial unique index and self-FK permit; applies per-item '
  'verdicts; drives submitted -> in_review -> approved / rejected / '
  'changes_requested. Grant EXECUTE only to the gate service role.';

-- ---------------------------------------------------------------------
-- hitl.edit_item -- the reviewer correction.
--
-- docs/07 calls this the most valuable event in the system: the delta
-- between what the agent proposed and what a human was willing to merge is
-- the supervision signal the whole training pipeline is built to harvest,
-- which is why original_payload is written once and never overwritten.
-- ---------------------------------------------------------------------
CREATE FUNCTION hitl.edit_item(p_item_id bigint, p_principal text, p_payload jsonb)
RETURNS hitl.proposal_item
LANGUAGE plpgsql AS $$
DECLARE
  it hitl.proposal_item;
  st hitl.proposal_status;
BEGIN
  SELECT * INTO it FROM hitl.proposal_item WHERE item_id = p_item_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'proposal item % not found', p_item_id;
  END IF;

  SELECT status INTO st FROM hitl.proposal WHERE proposal_id = it.proposal_id;
  IF st NOT IN ('submitted','in_review') THEN
    RAISE EXCEPTION 'proposal % is %, items are only editable while it is under '
                    'review', it.proposal_id, st;
  END IF;

  IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'payload must be a JSON object, got %',
                    coalesce(jsonb_typeof(p_payload), 'null');
  END IF;

  UPDATE hitl.proposal_item
     SET original_payload = coalesce(original_payload, payload),
         payload          = p_payload,
         edited_by        = p_principal,
         edited_at        = now(),
         item_status      = 'edited'
   WHERE item_id = p_item_id
  RETURNING * INTO it;

  RETURN it;
END $$;

-- ---------------------------------------------------------------------
-- hitl.apply_trace_label -- where the training label comes from.
--
-- 011:84-89 calls this the single most important privilege boundary in the
-- training pipeline: the label comes from the human gate, never from the
-- agent being trained. This function is the only thing that writes it.
--
--   merged + any item edited or dropped -> 'corrected'
--   merged, untouched                   -> 'accepted'
--   rejected                            -> 'rejected'
--
-- Called from record_decision on reject, and by the merge endpoint in the
-- same transaction as hitl.merge_proposal. A no-op when the proposal has
-- no trace_session_id (hand-written proposals) or has not reached a
-- terminal human judgment.
-- ---------------------------------------------------------------------
CREATE FUNCTION hitl.apply_trace_label(p_proposal_id bigint)
RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  p         hitl.proposal;
  v_outcome trn.trace_outcome;
BEGIN
  SELECT * INTO p FROM hitl.proposal WHERE proposal_id = p_proposal_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'proposal % not found', p_proposal_id;
  END IF;

  IF p.trace_session_id IS NULL THEN RETURN; END IF;
  IF p.status NOT IN ('merged','rejected') THEN RETURN; END IF;

  IF p.status = 'rejected' THEN
    v_outcome := 'rejected';
  ELSIF EXISTS (SELECT 1 FROM hitl.proposal_item
                 WHERE proposal_id = p_proposal_id
                   AND item_status IN ('edited','dropped')) THEN
    v_outcome := 'corrected';
  ELSE
    v_outcome := 'accepted';
  END IF;

  UPDATE trn.trace_session
     SET outcome           = v_outcome,
         label_proposal_id = p_proposal_id,
         label_source      = 'hitl_gate'
   WHERE session_id = p.trace_session_id;
END $$;

COMMENT ON FUNCTION hitl.apply_trace_label IS
  'Writes trn.trace_session.outcome from the human gate outcome '
  '(accepted / corrected / rejected). The only writer of the training '
  'label; granted to fde_gate_service alone.';

-- ---------------------------------------------------------------------
-- hitl.expire_proposals -- the hourly SLA sweep.
--
-- Deliberately does NOT label the trace. An expiry is an SLA breach, not a
-- human judgment: nobody looked at it, so nobody can say whether the agent
-- was right. outcome stays 'pending', which keeps the session out of
-- trn.sft_export by construction rather than by a filter someone has to
-- remember to write.
--
-- 'changes_requested' is included alongside 'submitted' and 'in_review':
-- those are exactly the three non-terminal states 004's proposal_queue_idx
-- covers, and a proposal handed back to the agent still has an expires_at
-- that means something.
-- ---------------------------------------------------------------------
CREATE FUNCTION hitl.expire_proposals()
RETURNS int
LANGUAGE sql AS $$
  WITH expired AS (
    UPDATE hitl.proposal
       SET status = 'expired', updated_at = now()
     WHERE status IN ('submitted','in_review','changes_requested')
       AND expires_at < now()
    RETURNING proposal_id
  )
  SELECT count(*)::int FROM expired;
$$;

COMMENT ON FUNCTION hitl.expire_proposals IS
  'Expires proposals past expires_at. Deliberately leaves '
  'trn.trace_session.outcome at ''pending'' -- an expiry is an SLA breach, '
  'not a label.';

-- =====================================================================
-- WORKFLOW TRANSITIONS
--
-- Same philosophy: the runner in packages/fde-gate is a thin caller. It
-- decides WHAT to execute; these functions decide what state that leaves
-- behind. In particular on_failure (halt/retry/skip/escalate) is
-- interpreted here, once, rather than in whichever process happened to
-- notice the failure.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Move a run's cursor to the next step, or finish it. Shared by
-- complete_step, fail_step (skip) and respond_human (skip) so the three
-- paths cannot disagree about what "next" means.
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.advance_cursor(p_run_id bigint, p_from_step_id bigint,
                                  p_goto_step_key text DEFAULT NULL)
RETURNS wf.run
LANGUAGE plpgsql AS $$
DECLARE
  r      wf.run;
  v_wf   bigint;
  v_ord  int;
  v_next bigint;
BEGIN
  SELECT workflow_id, ordinal INTO v_wf, v_ord FROM wf.step WHERE step_id = p_from_step_id;

  IF p_goto_step_key IS NOT NULL THEN
    SELECT step_id INTO v_next FROM wf.step
     WHERE workflow_id = v_wf AND step_key = p_goto_step_key;
    IF v_next IS NULL THEN
      RAISE EXCEPTION 'step_key % is not a step of workflow %; a decision branch '
                      'may only jump within its own workflow', p_goto_step_key, v_wf;
    END IF;
  ELSE
    SELECT step_id INTO v_next FROM wf.step
     WHERE workflow_id = v_wf AND ordinal > v_ord
     ORDER BY ordinal LIMIT 1;
  END IF;

  IF v_next IS NULL THEN
    UPDATE wf.run
       SET status = 'succeeded', current_step_id = NULL, finished_at = now()
     WHERE run_id = p_run_id
    RETURNING * INTO r;
  ELSE
    UPDATE wf.run
       SET status = 'running', current_step_id = v_next
     WHERE run_id = p_run_id
    RETURNING * INTO r;
  END IF;

  RETURN r;
END $$;

-- ---------------------------------------------------------------------
-- wf.publish_workflow -- B18. The ONLY publish path.
--
-- SECURITY DEFINER for the same reason hitl.merge_proposal is: no role
-- holds UPDATE on wf.workflow, and that absence is a CI invariant
-- (fde_agent must not be able to flip status to 'published'). Making
-- publish a function means the privilege is EXECUTE on one audited
-- transition rather than UPDATE on a table, so the gate service can
-- publish and still cannot, say, repoint pinned_commit_id.
--
-- assert_faithful runs INSIDE this function rather than being something a
-- caller is trusted to have called first -- an unfaithful workflow that
-- reaches 'published' is a workflow whose steps nobody in the business
-- actually performs.
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.publish_workflow(p_workflow_id bigint, p_published_by text)
RETURNS wf.workflow
LANGUAGE plpgsql SECURITY DEFINER SET search_path = wf, kg, public AS $$
DECLARE w wf.workflow;
BEGIN
  SELECT * INTO w FROM wf.workflow WHERE workflow_id = p_workflow_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'workflow % not found', p_workflow_id;
  END IF;
  IF w.status NOT IN ('draft','review') THEN
    RAISE EXCEPTION 'workflow % is %; only a draft or in-review workflow can be '
                    'published', p_workflow_id, w.status;
  END IF;

  PERFORM wf.assert_faithful(p_workflow_id);

  UPDATE wf.workflow
     SET status        = 'published',
         published_by  = p_published_by,
         published_at  = now(),
         -- Pin the digest as well as the commit id, so a tampered replay of
         -- the pinned commit is detectable (002:36-38).
         pinned_digest = (SELECT content_digest FROM kg.commit
                           WHERE commit_id = w.pinned_commit_id)
   WHERE workflow_id = p_workflow_id
  RETURNING * INTO w;

  RETURN w;
END $$;

REVOKE ALL ON FUNCTION wf.publish_workflow(bigint, text) FROM PUBLIC;

COMMENT ON FUNCTION wf.publish_workflow IS
  'Sole publish path. SECURITY DEFINER because no role holds UPDATE on '
  'wf.workflow. Requires draft/review status, runs wf.assert_faithful, and '
  'stamps published_by/published_at/pinned_digest. Grant EXECUTE only to '
  'the gate service role.';

-- ---------------------------------------------------------------------
-- wf.start_run
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.start_run(p_workflow_id bigint, p_started_by text,
                             p_input jsonb DEFAULT '{}'::jsonb,
                             p_groups text[] DEFAULT '{}')
RETURNS wf.run
LANGUAGE plpgsql AS $$
DECLARE
  w       wf.workflow;
  r       wf.run;
  v_first bigint;
  v_input jsonb := coalesce(p_input, '{}'::jsonb);
BEGIN
  SELECT * INTO w FROM wf.workflow WHERE workflow_id = p_workflow_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'workflow % not found', p_workflow_id;
  END IF;
  IF w.status <> 'published' THEN
    RAISE EXCEPTION 'workflow % is %, not published; publish it with '
                    'wf.publish_workflow before running it', p_workflow_id, w.status;
  END IF;

  -- Empty runnable_by means "anyone in product ops"; otherwise the caller
  -- must match by group or by principal.
  IF cardinality(w.runnable_by) > 0
     AND NOT (w.runnable_by && coalesce(p_groups, '{}'::text[]))
     AND NOT (p_started_by = ANY (w.runnable_by)) THEN
    RAISE EXCEPTION '% may not run workflow % (runnable_by = %)',
                    p_started_by, p_workflow_id, w.runnable_by;
  END IF;

  SELECT step_id INTO v_first FROM wf.step
   WHERE workflow_id = p_workflow_id ORDER BY ordinal LIMIT 1;
  IF v_first IS NULL THEN
    RAISE EXCEPTION 'workflow % has no steps', p_workflow_id;
  END IF;

  INSERT INTO wf.run (workflow_id, engagement_id, status, started_by, input,
                      context, current_step_id)
  VALUES (p_workflow_id, w.engagement_id, 'pending', p_started_by, v_input,
          -- Seeding context with the input is what makes {"$ctx": "$.input.x"}
          -- tool-arg templates and decision jsonpaths work on the first step.
          jsonb_build_object('input', v_input), v_first)
  RETURNING * INTO r;

  RETURN r;
END $$;

-- ---------------------------------------------------------------------
-- wf.begin_step -- also the runner's concurrency mutex.
--
-- The row lock on wf.run plus the open-step guard is what stops the
-- 1-minute EventBridge tick and a concurrent API-triggered advance from
-- executing the same step twice. Two callers race; one gets the run lock,
-- inserts the run_step and commits; the second then sees an open step and
-- raises rather than double-invoking an agent.
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.begin_step(p_run_id bigint)
RETURNS wf.run_step
LANGUAGE plpgsql AS $$
DECLARE
  r         wf.run;
  rs        wf.run_step;
  v_attempt int;
BEGIN
  SELECT * INTO r FROM wf.run WHERE run_id = p_run_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'run % not found', p_run_id;
  END IF;
  IF r.status NOT IN ('pending','running') THEN
    RAISE EXCEPTION 'run % is %, cannot begin a step', p_run_id, r.status;
  END IF;
  IF r.current_step_id IS NULL THEN
    RAISE EXCEPTION 'run % has no current step', p_run_id;
  END IF;

  IF EXISTS (SELECT 1 FROM wf.run_step
              WHERE run_id = p_run_id AND status IN ('running','awaiting_human')) THEN
    RAISE EXCEPTION 'run % already has an open step; complete, fail or respond '
                    'to it before beginning another', p_run_id;
  END IF;

  SELECT coalesce(max(attempt), 0) + 1 INTO v_attempt
    FROM wf.run_step WHERE run_id = p_run_id AND step_id = r.current_step_id;

  -- The step's input is the run context as of this attempt, so a retry after
  -- an intervening step edit is reproducible from the row itself.
  INSERT INTO wf.run_step (run_id, step_id, attempt, status, input)
  VALUES (p_run_id, r.current_step_id, v_attempt, 'running', r.context)
  RETURNING * INTO rs;

  UPDATE wf.run SET status = 'running' WHERE run_id = p_run_id;

  RETURN rs;
END $$;

COMMENT ON FUNCTION wf.begin_step IS
  'Opens the current step for execution and returns the run_step row. '
  'Raises if the run already has an open step -- that guard plus the run '
  'row lock is the runner''s concurrency mutex against the EventBridge '
  'tick racing an API-triggered advance.';

-- ---------------------------------------------------------------------
-- wf.complete_step
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.complete_step(p_run_step_id bigint, p_output jsonb DEFAULT NULL,
                                 p_goto_step_key text DEFAULT NULL)
RETURNS wf.run
LANGUAGE plpgsql AS $$
DECLARE
  rs wf.run_step;
  s  wf.step;
BEGIN
  SELECT * INTO rs FROM wf.run_step WHERE run_step_id = p_run_step_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'run step % not found', p_run_step_id;
  END IF;
  IF rs.status <> 'running' THEN
    RAISE EXCEPTION 'run step % is %, only a running step can be completed',
                    p_run_step_id, rs.status;
  END IF;

  SELECT * INTO s FROM wf.step WHERE step_id = rs.step_id;

  UPDATE wf.run_step
     SET status = 'succeeded', output = p_output, finished_at = now()
   WHERE run_step_id = p_run_step_id;

  -- Output lands in the run context under the step_key. That is the contract
  -- decision branches read with jsonb_path_match and tool steps read with
  -- {"$ctx": "..."} argument templates.
  UPDATE wf.run
     SET context = context || jsonb_build_object(s.step_key, coalesce(p_output, '{}'::jsonb))
   WHERE run_id = rs.run_id;

  RETURN wf.advance_cursor(rs.run_id, rs.step_id, p_goto_step_key);
END $$;

-- ---------------------------------------------------------------------
-- wf.fail_step -- where on_failure is interpreted.
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.fail_step(p_run_step_id bigint, p_error jsonb,
                             p_max_attempts int DEFAULT 3)
RETURNS wf.run
LANGUAGE plpgsql AS $$
DECLARE
  rs wf.run_step;
  s  wf.step;
  r  wf.run;
BEGIN
  SELECT * INTO rs FROM wf.run_step WHERE run_step_id = p_run_step_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'run step % not found', p_run_step_id;
  END IF;
  IF rs.status <> 'running' THEN
    RAISE EXCEPTION 'run step % is %, only a running step can fail',
                    p_run_step_id, rs.status;
  END IF;

  SELECT * INTO s FROM wf.step WHERE step_id = rs.step_id;

  -- The error is stored verbatim on the attempt. docs/10 §2 tells the
  -- operator to read it; paraphrasing it here would be paraphrasing the
  -- only evidence they have.
  UPDATE wf.run_step
     SET status = 'failed', error = p_error, finished_at = now()
   WHERE run_step_id = p_run_step_id;

  IF s.on_failure = 'halt' THEN
    UPDATE wf.run SET status = 'failed', error = p_error, finished_at = now()
     WHERE run_id = rs.run_id RETURNING * INTO r;

  ELSIF s.on_failure = 'retry' THEN
    IF rs.attempt >= p_max_attempts THEN
      UPDATE wf.run SET status = 'failed', error = p_error, finished_at = now()
       WHERE run_id = rs.run_id RETURNING * INTO r;
    ELSE
      -- current_step_id is left alone: the next begin_step opens attempt+1.
      UPDATE wf.run SET status = 'running'
       WHERE run_id = rs.run_id RETURNING * INTO r;
    END IF;

  ELSIF s.on_failure = 'skip' THEN
    r := wf.advance_cursor(rs.run_id, rs.step_id);

  ELSE  -- escalate
    -- A fresh attempt row parked on a human. It is not created by
    -- wf.await_human because that path is for kind='human' steps the author
    -- intended to stop at; this one is an exception a person now owns.
    INSERT INTO wf.run_step (run_id, step_id, attempt, status, input)
    VALUES (rs.run_id, rs.step_id, rs.attempt + 1, 'awaiting_human',
            jsonb_build_object('escalated_error', p_error));
    UPDATE wf.run SET status = 'awaiting_human'
     WHERE run_id = rs.run_id RETURNING * INTO r;
  END IF;

  RETURN r;
END $$;

COMMENT ON FUNCTION wf.fail_step IS
  'Records a step failure and applies wf.step.on_failure: halt fails the '
  'run, retry re-opens the same step until p_max_attempts, skip advances '
  'the cursor, escalate parks a new awaiting_human attempt for an operator.';

-- ---------------------------------------------------------------------
-- wf.await_human
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.await_human(p_run_step_id bigint,
                               p_awaiting_principal text DEFAULT NULL)
RETURNS wf.run_step
LANGUAGE plpgsql AS $$
DECLARE
  rs wf.run_step;
  s  wf.step;
BEGIN
  SELECT * INTO rs FROM wf.run_step WHERE run_step_id = p_run_step_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'run step % not found', p_run_step_id;
  END IF;
  IF rs.status <> 'running' THEN
    RAISE EXCEPTION 'run step % is %, only a running step can start waiting on a '
                    'human', p_run_step_id, rs.status;
  END IF;

  SELECT * INTO s FROM wf.step WHERE step_id = rs.step_id;
  IF s.kind <> 'human' THEN
    RAISE EXCEPTION 'step % is kind %, not human; failures escalate to a human '
                    'through wf.fail_step instead', s.step_key, s.kind;
  END IF;

  UPDATE wf.run_step
     SET status = 'awaiting_human', awaiting_principal = p_awaiting_principal
   WHERE run_step_id = p_run_step_id
  RETURNING * INTO rs;

  UPDATE wf.run SET status = 'awaiting_human' WHERE run_id = rs.run_id;

  RETURN rs;
END $$;

-- ---------------------------------------------------------------------
-- wf.cancel_run
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.cancel_run(p_run_id bigint, p_cancelled_by text)
RETURNS wf.run
LANGUAGE plpgsql AS $$
DECLARE r wf.run;
BEGIN
  SELECT * INTO r FROM wf.run WHERE run_id = p_run_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'run % not found', p_run_id;
  END IF;
  IF r.status IN ('succeeded','failed','cancelled') THEN
    RAISE EXCEPTION 'run % is already %, nothing to cancel', p_run_id, r.status;
  END IF;

  UPDATE wf.run_step
     SET status = 'cancelled', finished_at = now()
   WHERE run_id = p_run_id AND status IN ('pending','running','awaiting_human');

  UPDATE wf.run
     SET status = 'cancelled', finished_at = now(),
         error  = jsonb_build_object('cancelled_by', p_cancelled_by)
   WHERE run_id = p_run_id
  RETURNING * INTO r;

  RETURN r;
END $$;

-- ---------------------------------------------------------------------
-- wf.respond_human
--
-- One entry point for both flavours of awaiting_human: a kind='human' step
-- the author put there, and an escalated attempt parked by fail_step. The
-- action names are the operator's four real options in docs/10 §2.
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.respond_human(p_run_step_id bigint, p_responded_by text,
                                 p_response jsonb, p_action text)
RETURNS wf.run
LANGUAGE plpgsql AS $$
DECLARE
  rs wf.run_step;
  s  wf.step;
  r  wf.run;
BEGIN
  IF p_action NOT IN ('approve','retry','skip','abort') THEN
    RAISE EXCEPTION 'unknown action %; expected approve, retry, skip or abort',
                    p_action;
  END IF;

  SELECT * INTO rs FROM wf.run_step WHERE run_step_id = p_run_step_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'run step % not found', p_run_step_id;
  END IF;
  IF rs.status <> 'awaiting_human' THEN
    RAISE EXCEPTION 'run step % is %, not awaiting a human', p_run_step_id, rs.status;
  END IF;

  SELECT * INTO s FROM wf.step WHERE step_id = rs.step_id;

  -- Recorded whatever the action turns out to be: the operator's answer is
  -- audit trail and training data even when it aborts the run.
  UPDATE wf.run_step
     SET human_response = p_response, responded_by = p_responded_by,
         responded_at   = now()
   WHERE run_step_id = p_run_step_id;

  IF p_action = 'abort' THEN
    RETURN wf.cancel_run(rs.run_id, p_responded_by);
  END IF;

  IF p_action = 'approve' THEN
    IF s.kind <> 'human' THEN
      RAISE EXCEPTION 'step % is an escalated % step; respond with retry or skip, '
                      'not approve', s.step_key, s.kind;
    END IF;
    -- complete_step owns the context merge and the cursor advance; put the
    -- row back to 'running' for the one statement that insists on it, rather
    -- than duplicating that logic here.
    UPDATE wf.run_step SET status = 'running' WHERE run_step_id = p_run_step_id;
    RETURN wf.complete_step(p_run_step_id, p_response);
  END IF;

  UPDATE wf.run_step
     SET status = 'succeeded', finished_at = now()
   WHERE run_step_id = p_run_step_id;

  IF p_action = 'retry' THEN
    -- current_step_id unchanged: the runner re-executes the step on its next
    -- advance, as attempt+1.
    UPDATE wf.run SET status = 'running' WHERE run_id = rs.run_id RETURNING * INTO r;
    RETURN r;
  END IF;

  RETURN wf.advance_cursor(rs.run_id, rs.step_id);   -- skip
END $$;

-- ---------------------------------------------------------------------
-- wf.timeout_steps -- the 1-minute sweep.
--
-- Only 'running' steps are eligible. awaiting_human steps NEVER time out:
-- docs/10 §2 promises the operator that "the workflow will wait for you",
-- and a run that quietly failed while someone was at lunch is exactly the
-- behaviour that teaches operators not to trust the queue.
--
-- This is also what makes the runner's two-transaction pattern safe. A
-- process that crashes after begin_step and before complete_step leaves a
-- 'running' row; this sweep fails it at timeout_seconds and routes it
-- through on_failure, so a crash self-heals instead of stranding the run.
-- ---------------------------------------------------------------------
CREATE FUNCTION wf.timeout_steps()
RETURNS int
LANGUAGE plpgsql AS $$
DECLARE
  v_row record;
  n     int := 0;
BEGIN
  FOR v_row IN
    SELECT rs.run_step_id, s.timeout_seconds
      FROM wf.run_step rs
      JOIN wf.step s ON s.step_id = rs.step_id
     WHERE rs.status = 'running'
       AND rs.started_at + make_interval(secs => s.timeout_seconds) < now()
     ORDER BY rs.run_step_id
  LOOP
    PERFORM wf.fail_step(v_row.run_step_id,
                         jsonb_build_object('error', 'step timed out',
                                            'timeout_seconds', v_row.timeout_seconds));
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

COMMENT ON FUNCTION wf.timeout_steps IS
  'Fails running steps past their timeout_seconds through wf.fail_step. '
  'awaiting_human steps are never eligible -- human steps wait '
  'indefinitely by design (docs/10 §2).';

-- =====================================================================
-- GRANTS
--
-- Functions arrive with EXECUTE granted to PUBLIC. Left alone, that would
-- hand every role in the platform -- including fde_agent and
-- fde_rl_rollout -- the ability to record its own gate decision and
-- publish its own workflow, which is the entire boundary 004/005/010/011
-- exist to hold. Revoke first, grant by name.
-- =====================================================================
REVOKE ALL ON FUNCTION
  hitl.gate_quorum_met(bigint),
  hitl.record_decision(bigint, text, hitl.decision, text, jsonb, int),
  hitl.edit_item(bigint, text, jsonb),
  hitl.apply_trace_label(bigint),
  hitl.expire_proposals(),
  wf.advance_cursor(bigint, bigint, text),
  wf.publish_workflow(bigint, text),
  wf.start_run(bigint, text, jsonb, text[]),
  wf.begin_step(bigint),
  wf.complete_step(bigint, jsonb, text),
  wf.fail_step(bigint, jsonb, int),
  wf.await_human(bigint, text),
  wf.cancel_run(bigint, text),
  wf.respond_human(bigint, text, jsonb, text),
  wf.timeout_steps()
FROM PUBLIC;

-- The review domain. fde_gate_service already holds SELECT/INSERT/UPDATE on
-- every hitl table and the hitl sequences (010:48,52), so these run with the
-- caller's own privileges -- no SECURITY DEFINER needed and none wanted.
GRANT EXECUTE ON FUNCTION
  hitl.gate_quorum_met(bigint),
  hitl.record_decision(bigint, text, hitl.decision, text, jsonb, int),
  hitl.edit_item(bigint, text, jsonb),
  hitl.apply_trace_label(bigint),
  hitl.expire_proposals(),
  wf.publish_workflow(bigint, text)
TO fde_gate_service;

-- The run domain. fde_prodops already holds SELECT on all of wf,
-- INSERT/UPDATE on wf.run and wf.run_step, and the wf sequences
-- (010:66-67,72). Note what is NOT here: publish_workflow. Product ops runs
-- workflows; publishing one is a gated action (010:64).
GRANT EXECUTE ON FUNCTION
  wf.advance_cursor(bigint, bigint, text),
  wf.start_run(bigint, text, jsonb, text[]),
  wf.begin_step(bigint),
  wf.complete_step(bigint, jsonb, text),
  wf.fail_step(bigint, jsonb, int),
  wf.await_human(bigint, text),
  wf.cancel_run(bigint, text),
  wf.respond_human(bigint, text, jsonb, text),
  wf.timeout_steps()
TO fde_prodops;

-- B17. 011:87-89 granted fde_gate_service UPDATE on trn.trace_session's
-- label columns and SELECT on all of trn, but 010:47 granted it USAGE only
-- on kg, hitl, wf and sor. Table privileges without schema USAGE are
-- unreachable: hitl.apply_trace_label fails with "permission denied for
-- schema trn" as fde_gate_service (verified empirically), which would have
-- meant every merged proposal silently failing to label its trace -- the
-- one write the training pipeline cannot do without.
GRANT USAGE ON SCHEMA trn TO fde_gate_service;
