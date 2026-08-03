-- =====================================================================
-- 018_agent_launch_records.sql
-- Giving a console agent launch somewhere to leave a trace.
--
-- `/ui/agents/run` dispatches to an AgentCore runtime and waits for the
-- answer. The wait is bounded by FDE_GATE_STEP_TIMEOUT_SECONDS, but the
-- deployed front door is an API Gateway HTTP API whose integration times
-- out well before that -- so a long task returns an error to the browser
-- while the Lambda, and the agent, keep going. Until this migration
-- nothing recorded the launch at all, which made that failure mode
-- unanswerable: the operator could not learn whether the agent had run,
-- whether it had finished, or whether launching again would be a
-- duplicate. `service/agents.launch`'s own docstring said so, and named
-- the missing migration as the fix. This is it.
--
-- Why this is not a wf.run
-- ------------------------
-- The obvious move -- write the launch as a `wf.run` with a synthetic
-- workflow -- is wrong in the same way db/016's `admin` gate kind was.
-- `wf.run.workflow_id` is NOT NULL and references `wf.workflow`, and a
-- workflow is a published, faithfulness-checked, pinned artefact: every
-- step of it cites a graph element live at the pinned commit
-- (`wf.assert_faithful`). A console launch cites nothing and publishes
-- nothing. Giving it a placeholder workflow row would put an unpublished
-- fiction into the table `/ui/workflows` lists, make `wf.advance_run`'s
-- transitions reachable on a row with no steps, and leave every count of
-- "how many workflows does this engagement have" one too high.
--
-- So a launch gets its own table, sitting beside the runs rather than
-- pretending to be one, and it carries only what answers the operator's
-- question: who asked for what, when, and how it ended.
--
-- What this deliberately does NOT do
-- -----------------------------------
-- It does not make the launch asynchronous. The request still waits; the
-- record is what makes waiting survivable rather than what replaces it.
-- Returning immediately is a larger change (a dispatcher, a poller, and a
-- second place the payload contract could drift) and it is not what the
-- operator harm was: the harm was that a timed-out launch left nothing
-- behind to read.
-- =====================================================================

-- ---------------------------------------------------------------------
-- One row per launch the console accepted.
--
-- Written BEFORE dispatch on purpose. A row that appeared only on
-- completion would be absent in exactly the case this table exists for --
-- the request that died at the gateway while the agent kept running. So
-- the row is committed while the launch is still `queued`, and the
-- outcome is stamped onto it afterwards. A `running` row with no
-- `completed_at` is therefore a real and expected state, and it means
-- "dispatched, and nothing has reported back": either still running, or
-- the process that was waiting for it is gone. The console says exactly
-- that rather than guessing which.
-- ---------------------------------------------------------------------
CREATE TABLE wf.agent_launch (
  launch_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id uuid NOT NULL,
  -- The persona and the task, as the console sent them. Not FK'd to
  -- anything: the agent registry is Python (fde_agents' VALID_TASKS,
  -- mirrored in fde_gate.service.agents and drift-tested against it), and
  -- a lookup table here would be a third copy that could disagree with
  -- both. Text also keeps a historical row readable after a task is
  -- renamed, which an enum would not.
  agent         text NOT NULL,
  task          text NOT NULL,
  -- Who asked. `hitl.reviewer.principal` is the roster this is checked
  -- against before the row is written, but there is no FK: attribution
  -- must survive a reviewer being deactivated, and a FK would make
  -- "deactivate this person" fail against their launch history.
  principal     text NOT NULL,

  status        text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','succeeded','failed')),

  -- What was asked for, as the form resolved it. NOT the transcript --
  -- see the comment on the grants below.
  input         jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- The AgentCore session this launch was dispatched under, stamped when
  -- it is dispatched and left null when it never was. Null therefore
  -- means "there is no trace to go looking for", which is the useful
  -- reading: a launch refused for an unconfigured runtime never reached
  -- AWS at all.
  runtime_session_id text,

  -- The failure, verbatim, in the shape `StepExecutionError.detail`
  -- carries and `wf.run.error` stores -- docs/10 §2 tells an operator to
  -- read the error, and paraphrasing it here would throw away the only
  -- evidence they have.
  error         jsonb,

  requested_at  timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);

COMMENT ON TABLE wf.agent_launch IS
  'One row per agent task launched from the console (/ui/agents/run), '
  'written before dispatch so a launch that outlives its HTTP request '
  'still leaves a trace. Listed on /ui/runs. Not a wf.run: a launch has '
  'no workflow, no steps and no pinned commit.';

COMMENT ON COLUMN wf.agent_launch.status IS
  'queued (accepted, not yet dispatched) -> running (dispatched) -> '
  'succeeded | failed. A row left running means nothing reported back: '
  'either the agent is still working or the waiting process is gone.';

COMMENT ON COLUMN wf.agent_launch.input IS
  'The task input as the form resolved it, with the free-text material '
  'recorded as a character count rather than stored. See db/018.';

-- The engagement-scoped read, newest first -- the shape `list_launches`
-- asks for, and the same shape `run_queue_idx` serves for wf.run. The
-- console's unfiltered list scans instead, exactly as the runs list does;
-- both are bounded by LIMIT and neither is a table that grows per row of
-- graph.
CREATE INDEX agent_launch_recent_idx
  ON wf.agent_launch (engagement_id, requested_at DESC);

-- =====================================================================
-- GRANTS
--
-- A new table gets nothing from db/010's `ALL TABLES IN SCHEMA wf`: that
-- expanded once, when 010 ran. So this is the complete access list for
-- wf.agent_launch, and every other role -- fde_agent, fde_prodops,
-- fde_training, fde_rl_rollout, fde_ingest -- cannot so much as SELECT
-- it. The CI denial matrix asserts the two that matter from the outside
-- rather than trusting this paragraph.
-- =====================================================================

-- fde_gate_service is the role the console's launch path already runs as
-- (`service/agents.launch` opens its transaction with the gate role), so
-- the write lands where the authorisation already is: the same
-- transaction that checked the caller is an active reviewer is the one
-- that files the row.
GRANT SELECT, INSERT ON wf.agent_launch TO fde_gate_service;

-- The UPDATE is column-scoped from the start, the technique db/016:111
-- and db/011:33 use. What a launch RECORDS -- who asked, on which
-- engagement, for which agent and task, with what input -- is the
-- request, and the request does not change after it was made. All this
-- service may write afterwards is how it ended:
--
--   status              queued -> running -> succeeded | failed
--   runtime_session_id  stamped at dispatch
--   error               the failure detail, verbatim
--   completed_at        when it stopped
--
-- Deliberately absent, and asserted absent in CI: `principal`. Rewriting
-- it would re-point a launch at a person who did not make it, which is
-- the same class of harm as db/016 refusing the console `UPDATE` on
-- `hitl.reviewer.principal`. `agent`, `task`, `engagement_id` and
-- `input` are absent for the same reason one step down: a record of a
-- request that can be edited into a record of a different request is not
-- a record.
GRANT UPDATE (status, runtime_session_id, error, completed_at)
  ON wf.agent_launch TO fde_gate_service;

-- ---------------------------------------------------------------------
-- Deliberately NOT granted, listed so the omissions read as decisions:
--
--   * DELETE, to anyone, including fde_gate_service. A launch history
--     that can be deleted answers "did this run?" only until somebody
--     would rather it did not. This is db/016's rule for the reviewer
--     roster, for the same reason, and CI asserts it.
--   * Anything at all to fde_prodops. It is the narrower of the
--     console's two roles (db/010:64-71: run workflows, triage drift,
--     read the graph) and /ui/runs renders this section through the gate
--     role instead. db/017 declined to widen prodops for the same
--     reason -- "because it is also the console" would put a new
--     capability on the role chosen for being narrow.
--   * Anything at all to fde_agent. An agent that could INSERT here
--     could file a launch record attributing its own work to a human who
--     never asked for it, and one that could UPDATE could mark its own
--     launch succeeded. Both are asserted denied in CI.
--   * Sequence privileges. `launch_id` is GENERATED ALWAYS AS IDENTITY,
--     whose sequence is owned by the column and reachable through INSERT
--     on the table (db/017's note); a USAGE ON SEQUENCE here would grant
--     nothing and imply something.
--
-- No function is created in this file, so there is no default PUBLIC
-- EXECUTE to revoke (013:877, 016:98). Stated rather than omitted,
-- because "did they forget the REVOKE" is the first question a reviewer
-- of a grants migration should ask.
-- ---------------------------------------------------------------------
