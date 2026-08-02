-- =====================================================================
-- 006_workflows.sql
-- Workflows authored FROM the graph, pinned TO a commit, RUN by product ops.
--
-- "Faithful workflow" has a precise meaning here: every step in a published
-- workflow carries at least one binding to a kg node or edge in the pinned
-- commit. A step with no binding cannot be published. That is what stops the
-- Workflow Agent from inventing steps that sound plausible but that nobody
-- in the business actually does.
-- =====================================================================

CREATE TABLE wf.workflow (
  workflow_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workflow_uuid   uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  engagement_id   uuid NOT NULL,
  slug            text NOT NULL,
  version         int  NOT NULL DEFAULT 1,
  title           text NOT NULL,
  description     text,
  status          wf.workflow_status NOT NULL DEFAULT 'draft',

  -- The graph state this workflow is faithful to. Immutable once published.
  pinned_commit_id bigint NOT NULL REFERENCES kg.commit(commit_id),
  pinned_digest    text,                 -- copy of commit.content_digest at pin time

  -- The process node this workflow implements.
  root_process_key text NOT NULL,

  -- Who may run it. Product ops group names, resolved by the runner service.
  runnable_by     text[] NOT NULL DEFAULT '{}',
  -- Steps an agent may execute unattended, versus those that always stop for
  -- a human. Derived from the `automation` gate at authoring time.
  autonomy_level  text NOT NULL DEFAULT 'assisted'
                  CHECK (autonomy_level IN ('manual','assisted','supervised','autonomous')),

  authored_by     text NOT NULL,
  published_by    text,
  published_at    timestamptz,
  deprecated_at   timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (engagement_id, slug, version)
);
CREATE INDEX workflow_published_idx
  ON wf.workflow (engagement_id, slug) WHERE status = 'published';
CREATE INDEX workflow_pin_idx ON wf.workflow (pinned_commit_id);

CREATE TABLE wf.step (
  step_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workflow_id   bigint NOT NULL REFERENCES wf.workflow(workflow_id) ON DELETE CASCADE,
  step_key      text   NOT NULL,
  ordinal       int    NOT NULL,
  kind          wf.step_kind NOT NULL,
  title         text   NOT NULL,
  instruction   text   NOT NULL,   -- what the operator or agent actually does
  -- For kind='tool'/'agent': the MCP tool name and argument template.
  tool_name     text,
  tool_args     jsonb,
  -- For kind='human': the question put to the operator and the accepted answers.
  human_prompt  text,
  human_schema  jsonb,
  -- For kind='decision': branch conditions as SQL/JSON path over run context.
  branches      jsonb,
  -- For kind='sor_write': which adapter and which write op.
  sor_adapter_key text,
  sor_write_op    text,
  -- Guardrail: this step may never run unattended.
  requires_human boolean NOT NULL DEFAULT false,
  timeout_seconds int NOT NULL DEFAULT 900,
  on_failure    text NOT NULL DEFAULT 'halt'
                CHECK (on_failure IN ('halt','retry','skip','escalate')),
  UNIQUE (workflow_id, step_key),
  UNIQUE (workflow_id, ordinal)
);

-- Faithfulness bindings. THE constraint that makes a workflow grounded.
CREATE TABLE wf.step_binding (
  binding_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  step_id       bigint NOT NULL REFERENCES wf.step(step_id) ON DELETE CASCADE,
  subject_kind  text   NOT NULL CHECK (subject_kind IN ('node','edge')),
  subject_key   text   NOT NULL,
  -- Why this step corresponds to that graph element.
  relation      text   NOT NULL CHECK (relation IN
                  ('implements','enforces','records_to','depends_on','measured_by')),
  -- Snapshot of the bound element's label at pin time, so a reviewer reading
  -- the workflow six months later sees what the author saw.
  pinned_label  text,
  UNIQUE (step_id, subject_kind, subject_key, relation)
);
CREATE INDEX step_binding_subject_idx ON wf.step_binding (subject_kind, subject_key);

-- Publication gate: refuse to publish an ungrounded workflow.
CREATE FUNCTION wf.assert_faithful(p_workflow_id bigint)
RETURNS void
LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_pin bigint;
  v_eng uuid;
  v_unbound text[];
  v_dangling text[];
BEGIN
  SELECT pinned_commit_id, engagement_id INTO v_pin, v_eng
    FROM wf.workflow WHERE workflow_id = p_workflow_id;

  -- 1. Every non-notify step must have at least one binding.
  SELECT array_agg(s.step_key ORDER BY s.ordinal) INTO v_unbound
    FROM wf.step s
   WHERE s.workflow_id = p_workflow_id
     AND s.kind <> 'notify'
     AND NOT EXISTS (SELECT 1 FROM wf.step_binding b WHERE b.step_id = s.step_id);

  IF v_unbound IS NOT NULL THEN
    RAISE EXCEPTION 'workflow % has unbound steps: % -- every step must cite a '
                    'graph element it implements', p_workflow_id, v_unbound;
  END IF;

  -- 2. Every bound key must exist and be live as of the pinned commit.
  SELECT array_agg(DISTINCT b.subject_key) INTO v_dangling
    FROM wf.step s
    JOIN wf.step_binding b ON b.step_id = s.step_id
   WHERE s.workflow_id = p_workflow_id
     AND NOT EXISTS (
       SELECT 1 FROM kg.node n
        WHERE b.subject_kind = 'node' AND n.engagement_id = v_eng
          AND n.node_key = b.subject_key AND n.commit_id <= v_pin
          AND (n.valid_to IS NULL OR n.valid_to > (SELECT sealed_at FROM kg.commit WHERE commit_id = v_pin))
       UNION ALL
       SELECT 1 FROM kg.edge e
        WHERE b.subject_kind = 'edge' AND e.engagement_id = v_eng
          AND e.edge_key = b.subject_key AND e.commit_id <= v_pin
          AND (e.valid_to IS NULL OR e.valid_to > (SELECT sealed_at FROM kg.commit WHERE commit_id = v_pin))
     );

  IF v_dangling IS NOT NULL THEN
    RAISE EXCEPTION 'workflow % binds keys not live at commit %: %',
                    p_workflow_id, v_pin, v_dangling;
  END IF;
END $$;

-- ---------------------------------------------------------------------
-- Runs -- what the product operations group actually executes
-- ---------------------------------------------------------------------
CREATE TABLE wf.run (
  run_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_uuid      uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  workflow_id   bigint NOT NULL REFERENCES wf.workflow(workflow_id),
  engagement_id uuid   NOT NULL,
  status        wf.run_status NOT NULL DEFAULT 'pending',
  -- Who kicked it off (product ops operator principal) and on whose behalf.
  started_by    text   NOT NULL,
  input         jsonb  NOT NULL DEFAULT '{}'::jsonb,
  context       jsonb  NOT NULL DEFAULT '{}'::jsonb,   -- accumulates step outputs
  -- The AgentCore runtime session this run is bound to. One run == one
  -- runtimeSessionId, so traces, memory, and observability all line up.
  runtime_session_id text,
  agent_runtime_arn  text,
  current_step_id    bigint REFERENCES wf.step(step_id),
  error         jsonb,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX run_queue_idx ON wf.run (engagement_id, status, started_at DESC);
CREATE INDEX run_session_idx ON wf.run (runtime_session_id);

CREATE TABLE wf.run_step (
  run_step_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id        bigint NOT NULL REFERENCES wf.run(run_id) ON DELETE CASCADE,
  step_id       bigint NOT NULL REFERENCES wf.step(step_id),
  attempt       int    NOT NULL DEFAULT 1,
  status        wf.run_status NOT NULL DEFAULT 'pending',
  input         jsonb,
  output        jsonb,
  -- For human steps: the async task id returned by
  -- BedrockAgentCoreApp.add_async_task(), so the runtime reports HealthyBusy
  -- while the operator is thinking rather than getting its session reaped.
  async_task_id bigint,
  awaiting_principal text,
  human_response jsonb,
  responded_by  text,
  responded_at  timestamptz,
  error         jsonb,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  UNIQUE (run_id, step_id, attempt)
);
CREATE INDEX run_step_awaiting_idx
  ON wf.run_step (awaiting_principal, started_at)
  WHERE status = 'awaiting_human';
