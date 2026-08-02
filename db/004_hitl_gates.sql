-- =====================================================================
-- 004_hitl_gates.sql
-- Deterministic human-in-the-loop: proposals, gate policy, decisions, merge
--
-- THE CORE INVARIANT OF THIS PLATFORM
--   Agents propose. Humans dispose. Only the merge function writes to kg.*.
--
-- "Deterministic" here means specifically: given a proposal's contents, the
-- set of gates required to merge it is computed by a pure SQL function over
-- a policy table -- not by an LLM, not by a heuristic in application code.
-- The same proposal always requires the same gates. Reviewers can be
-- replaced; the gate set cannot be negotiated by the agent that authored
-- the proposal. This is what makes the graph auditable and what gives the
-- training pipeline a trustworthy label (see 006).
-- =====================================================================

-- ---------------------------------------------------------------------
-- Reviewers. The only place a real human identity is stored.
-- ---------------------------------------------------------------------
CREATE TABLE hitl.reviewer (
  reviewer_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  principal     text NOT NULL UNIQUE,       -- IdP subject (Cognito sub / SAML NameID)
  display_name  text NOT NULL,
  email         text,
  -- Which gate kinds this person is authorised to clear, per engagement.
  is_active     boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE hitl.reviewer_authority (
  reviewer_id   bigint NOT NULL REFERENCES hitl.reviewer(reviewer_id),
  engagement_id uuid   NOT NULL,
  gate_kind     hitl.gate_kind NOT NULL,
  -- Scope restriction: NULL = all node types, else only these.
  node_types    kg.node_type[],
  granted_by    text NOT NULL,
  granted_at    timestamptz NOT NULL DEFAULT now(),
  revoked_at    timestamptz,
  PRIMARY KEY (reviewer_id, engagement_id, gate_kind)
);
CREATE INDEX reviewer_authority_active_idx
  ON hitl.reviewer_authority (engagement_id, gate_kind) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------
-- Gate policy -- the deterministic rule table.
--
-- Each row says: "a proposal item matching THIS predicate requires a gate of
-- THIS kind, cleared by THIS many distinct authorised reviewers."
-- Rules are additive: an item matching three rules requires all three gates.
--
-- match_node_types / match_edge_types: NULL means "any".
-- match_jsonpath: an optional SQL/JSON path evaluated against the item
--   payload, e.g. '$.attributes.touches_customer_data == true'.
-- ---------------------------------------------------------------------
CREATE TABLE hitl.gate_policy (
  policy_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id   uuid,                       -- NULL = applies to all engagements
  name            text NOT NULL,
  gate_kind       hitl.gate_kind NOT NULL,
  match_op        text[] CHECK (match_op <@ ARRAY['add_node','update_node','retire_node',
                                                  'add_edge','update_edge','retire_edge']),
  match_node_types kg.node_type[],
  match_edge_types kg.edge_type[],
  match_jsonpath  text,
  -- Below this evidence strength the gate fires even if it otherwise would not.
  min_evidence_strength real CHECK (min_evidence_strength BETWEEN 0 AND 1),
  quorum          int  NOT NULL DEFAULT 1 CHECK (quorum >= 1),
  -- Whether the proposing agent's own principal may count toward quorum.
  allow_self      boolean NOT NULL DEFAULT false,
  sla_hours       int  NOT NULL DEFAULT 72,
  is_active       boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX gate_policy_active_idx ON hitl.gate_policy (engagement_id) WHERE is_active;

-- ---------------------------------------------------------------------
-- Proposals
-- ---------------------------------------------------------------------
CREATE TABLE hitl.proposal (
  proposal_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  proposal_uuid  uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  engagement_id  uuid NOT NULL,
  title          text NOT NULL,
  rationale      text NOT NULL,             -- why the agent believes this
  status         hitl.proposal_status NOT NULL DEFAULT 'draft',
  -- Which agent produced it, and the exact runtime version. Required for
  -- attributing training labels back to a model revision.
  authored_by    text   NOT NULL,           -- AgentCore runtime ARN + qualifier
  agent_name     text   NOT NULL,           -- 'engagement' | 'workflow' | 'development'
  model_id       text,
  -- The graph state the agent reasoned over. Makes review reproducible.
  base_commit_id bigint REFERENCES kg.commit(commit_id),
  -- Links the proposal back to the agent trace that produced it (see 006).
  trace_session_id uuid,
  -- Set on merge.
  merged_commit_id bigint REFERENCES kg.commit(commit_id),
  submitted_at   timestamptz,
  decided_at     timestamptz,
  expires_at     timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX proposal_queue_idx
  ON hitl.proposal (engagement_id, status, submitted_at)
  WHERE status IN ('submitted','in_review','changes_requested');
CREATE INDEX proposal_trace_idx ON hitl.proposal (trace_session_id);

-- Individual mutations inside a proposal.
CREATE TABLE hitl.proposal_item (
  item_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  proposal_id   bigint NOT NULL REFERENCES hitl.proposal(proposal_id) ON DELETE CASCADE,
  ordinal       int    NOT NULL,
  op            text   NOT NULL CHECK (op IN ('add_node','update_node','retire_node',
                                              'add_edge','update_edge','retire_edge')),
  node_type     kg.node_type,
  edge_type     kg.edge_type,
  subject_key   text   NOT NULL,          -- node_key or edge_key being created/changed
  -- Full desired end state of the row, validated against a JSON schema in
  -- the gate service before the proposal may be submitted.
  payload       jsonb  NOT NULL,
  -- What the agent thinks this replaces. Populated by dedup search.
  supersedes_key text,
  -- Evidence must be attached BEFORE submit. Enforced by hitl.submit_proposal.
  source_ids    bigint[] NOT NULL DEFAULT '{}',
  agent_confidence real NOT NULL CHECK (agent_confidence BETWEEN 0 AND 1),
  -- Reviewer may edit the payload in place; the original is kept for training.
  original_payload jsonb,
  edited_by     text,
  edited_at     timestamptz,
  item_status   text NOT NULL DEFAULT 'pending'
                CHECK (item_status IN ('pending','accepted','edited','dropped')),
  UNIQUE (proposal_id, ordinal),
  CONSTRAINT item_type_matches_op CHECK (
    (op LIKE '%node' AND node_type IS NOT NULL AND edge_type IS NULL)
    OR (op LIKE '%edge' AND edge_type IS NOT NULL AND node_type IS NULL)
  )
);
CREATE INDEX proposal_item_proposal_idx ON hitl.proposal_item (proposal_id, ordinal);

-- ---------------------------------------------------------------------
-- Required gates -- MATERIALISED at submit time by hitl.compute_required_gates.
-- Materialising rather than computing on read means the gate set is frozen
-- against later policy edits, so an in-flight review cannot have its bar
-- moved underneath it.
-- ---------------------------------------------------------------------
CREATE TABLE hitl.proposal_gate (
  gate_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  proposal_id   bigint NOT NULL REFERENCES hitl.proposal(proposal_id) ON DELETE CASCADE,
  policy_id     bigint NOT NULL REFERENCES hitl.gate_policy(policy_id),
  gate_kind     hitl.gate_kind NOT NULL,
  quorum        int    NOT NULL,
  allow_self    boolean NOT NULL,
  -- Which items triggered this gate. Shown to the reviewer so they know
  -- exactly what they are signing off on.
  triggering_items bigint[] NOT NULL,
  due_at        timestamptz NOT NULL,
  cleared_at    timestamptz,
  UNIQUE (proposal_id, policy_id)
);
CREATE INDEX proposal_gate_open_idx
  ON hitl.proposal_gate (due_at) WHERE cleared_at IS NULL;

-- ---------------------------------------------------------------------
-- Decisions. Append-only. A reviewer changing their mind adds a row.
-- ---------------------------------------------------------------------
CREATE TABLE hitl.gate_decision (
  decision_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  gate_id       bigint NOT NULL REFERENCES hitl.proposal_gate(gate_id) ON DELETE CASCADE,
  reviewer_id   bigint NOT NULL REFERENCES hitl.reviewer(reviewer_id),
  decision      hitl.decision NOT NULL,
  comment       text,
  -- Per-item verdicts, so a reviewer can approve 7 of 9 items. Shape:
  -- {"<item_id>": {"verdict":"accept"|"drop"|"edit", "payload": {...}}}
  item_verdicts jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- Wall-clock the reviewer spent. A quality signal for the training set:
  -- a 4-second approval on a 30-item proposal is a rubber stamp, not a label.
  review_seconds int,
  decided_at    timestamptz NOT NULL DEFAULT now(),
  -- One live decision per (gate, reviewer); superseded ones keep history.
  superseded_by bigint REFERENCES hitl.gate_decision(decision_id)
);
CREATE UNIQUE INDEX gate_decision_live_uq
  ON hitl.gate_decision (gate_id, reviewer_id) WHERE superseded_by IS NULL;

-- =====================================================================
-- THE DETERMINISTIC GATE COMPUTATION
-- Pure function of (proposal contents, active policy). No model in the loop.
-- =====================================================================
CREATE FUNCTION hitl.compute_required_gates(p_proposal_id bigint)
RETURNS TABLE (policy_id bigint, gate_kind hitl.gate_kind, quorum int,
               allow_self boolean, sla_hours int, triggering_items bigint[])
LANGUAGE sql STABLE AS $$
  WITH prop AS (
    SELECT proposal_id, engagement_id FROM hitl.proposal WHERE proposal_id = p_proposal_id
  ),
  items AS (
    SELECT i.*, p.engagement_id
      FROM hitl.proposal_item i JOIN prop p USING (proposal_id)
  ),
  matched AS (
    SELECT gp.policy_id, gp.gate_kind, gp.quorum, gp.allow_self, gp.sla_hours, i.item_id
      FROM hitl.gate_policy gp
      JOIN items i
        ON (gp.engagement_id IS NULL OR gp.engagement_id = i.engagement_id)
       AND (gp.match_op        IS NULL OR i.op        = ANY(gp.match_op))
       AND (gp.match_node_types IS NULL OR i.node_type = ANY(gp.match_node_types))
       AND (gp.match_edge_types IS NULL OR i.edge_type = ANY(gp.match_edge_types))
       AND (gp.match_jsonpath  IS NULL OR jsonb_path_match(i.payload, gp.match_jsonpath::jsonpath))
       AND (gp.min_evidence_strength IS NULL
            OR kg.evidence_strength(i.engagement_id,
                                    CASE WHEN i.op LIKE '%node' THEN 'node' ELSE 'edge' END,
                                    i.subject_key) < gp.min_evidence_strength)
     WHERE gp.is_active
  )
  SELECT policy_id, gate_kind, quorum, allow_self, sla_hours,
         array_agg(item_id ORDER BY item_id)
    FROM matched
   GROUP BY policy_id, gate_kind, quorum, allow_self, sla_hours;
$$;

-- Submit: validate, freeze the gate set, move to 'submitted'.
-- Raises rather than returning an error code -- a half-submitted proposal is
-- worse than a failed one.
CREATE FUNCTION hitl.submit_proposal(p_proposal_id bigint)
RETURNS hitl.proposal
LANGUAGE plpgsql AS $$
DECLARE
  p hitl.proposal;
  n_items int;
  n_unsourced int;
  g record;
BEGIN
  SELECT * INTO p FROM hitl.proposal WHERE proposal_id = p_proposal_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'proposal % not found', p_proposal_id; END IF;
  IF p.status <> 'draft' AND p.status <> 'changes_requested' THEN
    RAISE EXCEPTION 'proposal % is %, cannot submit', p_proposal_id, p.status;
  END IF;

  SELECT count(*), count(*) FILTER (WHERE cardinality(source_ids) = 0
                                      AND op NOT LIKE 'retire%')
    INTO n_items, n_unsourced
    FROM hitl.proposal_item WHERE proposal_id = p_proposal_id;

  IF n_items = 0 THEN
    RAISE EXCEPTION 'proposal % has no items', p_proposal_id;
  END IF;
  IF n_unsourced > 0 THEN
    RAISE EXCEPTION 'proposal % has % item(s) with no evidence source; '
                    'every asserted fact must cite a kg.source', p_proposal_id, n_unsourced;
  END IF;

  DELETE FROM hitl.proposal_gate WHERE proposal_id = p_proposal_id;
  FOR g IN SELECT * FROM hitl.compute_required_gates(p_proposal_id) LOOP
    INSERT INTO hitl.proposal_gate
      (proposal_id, policy_id, gate_kind, quorum, allow_self, triggering_items, due_at)
    VALUES (p_proposal_id, g.policy_id, g.gate_kind, g.quorum, g.allow_self,
            g.triggering_items, now() + make_interval(hours => g.sla_hours));
  END LOOP;

  -- A proposal that matches no policy still requires one ontology gate.
  -- Fail-closed: never let a policy gap become an unreviewed auto-merge.
  IF NOT EXISTS (SELECT 1 FROM hitl.proposal_gate WHERE proposal_id = p_proposal_id) THEN
    RAISE EXCEPTION 'no gate policy matched proposal %; refusing to submit ungated '
                    '(add a catch-all policy rather than bypassing this check)', p_proposal_id;
  END IF;

  UPDATE hitl.proposal
     SET status = 'submitted', submitted_at = now(), updated_at = now(),
         expires_at = (SELECT max(due_at) FROM hitl.proposal_gate WHERE proposal_id = p_proposal_id)
   WHERE proposal_id = p_proposal_id
  RETURNING * INTO p;
  RETURN p;
END $$;

-- Is every required gate satisfied? Pure predicate; the merge function calls it.
CREATE FUNCTION hitl.gates_satisfied(p_proposal_id bigint)
RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT NOT EXISTS (
    SELECT 1
      FROM hitl.proposal_gate g
     WHERE g.proposal_id = p_proposal_id
       AND (
         -- not enough distinct approvals from authorised reviewers
         (SELECT count(DISTINCT d.reviewer_id)
            FROM hitl.gate_decision d
            JOIN hitl.reviewer r ON r.reviewer_id = d.reviewer_id
            JOIN hitl.reviewer_authority ra
              ON ra.reviewer_id = d.reviewer_id
             AND ra.gate_kind   = g.gate_kind
             AND ra.revoked_at IS NULL
             AND ra.engagement_id = (SELECT engagement_id FROM hitl.proposal
                                      WHERE proposal_id = p_proposal_id)
           WHERE d.gate_id = g.gate_id
             AND d.decision = 'approve'
             AND d.superseded_by IS NULL
             AND r.is_active
             AND (g.allow_self OR r.principal <> (SELECT authored_by FROM hitl.proposal
                                                   WHERE proposal_id = p_proposal_id))
         ) < g.quorum
         -- or anyone has an outstanding reject / changes_requested
         OR EXISTS (SELECT 1 FROM hitl.gate_decision d
                     WHERE d.gate_id = g.gate_id
                       AND d.superseded_by IS NULL
                       AND d.decision IN ('reject','request_changes'))
       )
  );
$$;

COMMENT ON FUNCTION hitl.gates_satisfied IS
  'Fail-closed quorum check. Returns false if ANY required gate lacks quorum '
  'from distinct, currently-authorised, non-self reviewers, or if any live '
  'decision is a reject/request_changes.';
