-- =====================================================================
-- 007_drift.sql
-- System-of-record adapters and autonomous drift detection.
--
-- Drift is measured on two axes, and they are genuinely different problems:
--
--   AXIS 1 -- REALITY DRIFT: the graph asserts how work is done; the SoR
--     shows how work is actually done. Divergence means the graph is stale
--     (or the business changed). Resolution: raise a hitl.proposal.
--
--   AXIS 2 -- PIN DRIFT: a published workflow was authored against commit N;
--     the graph is now at commit N+k and some bound element changed under it.
--     Resolution: re-author the workflow against head.
--
-- Both land in sor.drift_signal. The Workflow Agent triages; a human decides.
-- The agent never silently edits a published workflow.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Pluggable adapter registry. The blueprint is deliberately SoR-agnostic:
-- Jira, Salesforce, ServiceNow, an internal Postgres, an EventBridge stream
-- all register the same way. An adapter's only job is to emit normalised
-- observations into sor.observation.
-- ---------------------------------------------------------------------
CREATE TABLE sor.adapter (
  adapter_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id  uuid NOT NULL,
  adapter_key    text NOT NULL,          -- 'jira-prod', 'sfdc-cpq', 'anaplan'
  system_node_key text NOT NULL,         -- the kg 'system' node this adapter observes
  kind           text NOT NULL CHECK (kind IN
                   ('rest_poll','webhook','event_stream','db_cdc','warehouse_query')),
  -- Connection lives in Secrets Manager; only the ARN is stored here.
  secret_arn     text,
  -- How to map raw SoR records onto graph vocabulary. Authored by a human
  -- during onboarding, NOT inferred by an agent -- this mapping is the
  -- contract that makes drift measurable.
  --   {"activity_field":"status", "activity_map":{"In Review":"act.legal_review"},
  --    "actor_field":"assignee.accountId", "timestamp_field":"updated",
  --    "case_id_field":"key"}
  mapping        jsonb NOT NULL,
  poll_cron      text,
  is_active      boolean NOT NULL DEFAULT true,
  last_synced_at timestamptz,
  last_cursor    text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (engagement_id, adapter_key)
);

-- Normalised observations. One row per observed activity occurrence.
-- This is the "how work is actually done" table.
CREATE TABLE sor.observation (
  observation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id  uuid   NOT NULL,
  adapter_id     bigint NOT NULL REFERENCES sor.adapter(adapter_id),
  -- The business case this observation belongs to (ticket key, opportunity id).
  case_ref       text   NOT NULL,
  -- Mapped graph vocabulary. NULL activity_key means the SoR showed something
  -- the graph does not model -- itself a drift signal (missing_in_graph).
  activity_key   text,
  raw_activity   text   NOT NULL,
  -- Opaque, salted hash. Never a name, never an email.
  actor_hash     text,
  actor_role_key text,                   -- resolved role, when derivable
  system_object_key text,
  occurred_at    timestamptz NOT NULL,
  duration_seconds int,
  attributes     jsonb NOT NULL DEFAULT '{}'::jsonb,
  ingested_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX observation_case_idx
  ON sor.observation (engagement_id, case_ref, occurred_at);
CREATE INDEX observation_activity_idx
  ON sor.observation (engagement_id, activity_key, occurred_at DESC);
CREATE INDEX observation_recent_idx
  ON sor.observation (engagement_id, occurred_at DESC);

-- Observed transitions, derived from consecutive observations on a case.
-- This materialised view is what gets compared against `precedes` edges.
CREATE MATERIALIZED VIEW sor.observed_transition AS
WITH ordered AS (
  SELECT engagement_id, case_ref, activity_key, actor_role_key, occurred_at,
         lead(activity_key)   OVER w AS next_activity,
         lead(actor_role_key) OVER w AS next_role,
         lead(occurred_at)    OVER w AS next_at
    FROM sor.observation
   WHERE activity_key IS NOT NULL
  WINDOW w AS (PARTITION BY engagement_id, case_ref ORDER BY occurred_at)
)
SELECT engagement_id,
       activity_key AS src_key,
       next_activity AS dst_key,
       count(*)                                        AS observed_count,
       avg(extract(epoch FROM (next_at - occurred_at)))::real AS avg_gap_seconds,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (next_at - occurred_at)))::real AS p50_gap_seconds,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM (next_at - occurred_at)))::real AS p95_gap_seconds,
       min(occurred_at) AS first_seen,
       max(occurred_at) AS last_seen
  FROM ordered
 WHERE next_activity IS NOT NULL
 GROUP BY engagement_id, activity_key, next_activity;

CREATE UNIQUE INDEX observed_transition_uq
  ON sor.observed_transition (engagement_id, src_key, dst_key);

-- ---------------------------------------------------------------------
-- Drift signals
-- ---------------------------------------------------------------------
CREATE TABLE sor.drift_signal (
  signal_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id uuid NOT NULL,
  drift_kind    sor.drift_kind NOT NULL,
  severity      sor.drift_severity NOT NULL,
  state         sor.drift_state NOT NULL DEFAULT 'open',
  subject_kind  text NOT NULL CHECK (subject_kind IN ('node','edge','workflow','control')),
  subject_ref   text NOT NULL,
  -- Everything a reviewer needs to judge it without leaving the queue:
  -- expected vs observed, sample size, confidence, example case refs.
  detail        jsonb NOT NULL,
  -- Statistical backing. A drift signal with n=3 is noise; the triage rules
  -- in the Workflow Agent require min_samples before escalating severity.
  sample_size   int,
  effect_size   real,
  detected_at   timestamptz NOT NULL DEFAULT now(),
  -- Deduplication: the same drift re-detected updates last_seen rather than
  -- spamming the queue. Product ops will ignore a noisy queue.
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  occurrences   int NOT NULL DEFAULT 1,
  -- Triage
  triaged_by    text,
  triaged_at    timestamptz,
  raised_proposal_id bigint REFERENCES hitl.proposal(proposal_id),
  resolution_note text,
  resolved_at   timestamptz
);
CREATE UNIQUE INDEX drift_signal_dedup_uq
  ON sor.drift_signal (engagement_id, drift_kind, subject_kind, subject_ref)
  WHERE state IN ('open','triaged','proposal_raised');
CREATE INDEX drift_signal_queue_idx
  ON sor.drift_signal (engagement_id, severity DESC, detected_at DESC)
  WHERE state = 'open';

-- Upsert helper used by the detectors below and by the Workflow Agent's
-- monitoring tool. Dedups on the natural key.
CREATE OR REPLACE FUNCTION sor.record_drift(
  p_engagement uuid, p_kind sor.drift_kind, p_severity sor.drift_severity,
  p_subject_kind text, p_subject_ref text, p_detail jsonb,
  p_sample_size int DEFAULT NULL, p_effect_size real DEFAULT NULL)
RETURNS bigint
LANGUAGE sql AS $$
  INSERT INTO sor.drift_signal (engagement_id, drift_kind, severity, subject_kind,
                                subject_ref, detail, sample_size, effect_size)
  VALUES (p_engagement, p_kind, p_severity, p_subject_kind, p_subject_ref,
          p_detail, p_sample_size, p_effect_size)
  ON CONFLICT (engagement_id, drift_kind, subject_kind, subject_ref)
    WHERE state IN ('open','triaged','proposal_raised')
  DO UPDATE SET last_seen_at = now(),
                occurrences  = sor.drift_signal.occurrences + 1,
                detail       = EXCLUDED.detail,
                sample_size  = EXCLUDED.sample_size,
                effect_size  = EXCLUDED.effect_size,
                severity     = greatest(sor.drift_signal.severity, EXCLUDED.severity)
  RETURNING signal_id;
$$;

-- =====================================================================
-- DETECTORS
-- Deterministic SQL, run on a schedule. The agent does NOT decide whether
-- drift exists -- SQL does. The agent decides what to DO about it. Keeping
-- detection in SQL means drift findings are reproducible and cheap, and it
-- keeps the agent's job (explain, propose, prioritise) inside its competence.
-- =====================================================================

-- Detector 1: sequence drift + missing_in_sor.
-- Compares `precedes`/`hands_off_to` edges against observed transitions.
CREATE OR REPLACE FUNCTION sor.detect_sequence_drift(
  p_engagement uuid, p_min_samples int DEFAULT 20, p_lookback interval DEFAULT '90 days')
RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int := 0; r record;
BEGIN
  -- 1a. Graph asserts a transition the SoR essentially never shows.
  FOR r IN
    SELECT e.edge_key, e.src_key, e.dst_key,
           coalesce(ot.observed_count, 0) AS observed,
           (SELECT count(*) FROM sor.observation o
             WHERE o.engagement_id = p_engagement AND o.activity_key = e.src_key
               AND o.occurred_at > now() - p_lookback) AS src_occurrences
      FROM kg.edge_current e
      LEFT JOIN sor.observed_transition ot
        ON ot.engagement_id = e.engagement_id
       AND ot.src_key = e.src_key AND ot.dst_key = e.dst_key
     WHERE e.engagement_id = p_engagement
       AND e.edge_type IN ('precedes','hands_off_to')
  LOOP
    IF r.src_occurrences >= p_min_samples
       AND r.observed::real / greatest(r.src_occurrences,1) < 0.05 THEN
      PERFORM sor.record_drift(
        p_engagement, 'missing_in_sor',
        (CASE WHEN r.observed = 0 THEN 'high' ELSE 'medium' END)::sor.drift_severity,
        'edge', r.edge_key,
        jsonb_build_object('expected_transition', r.src_key || ' -> ' || r.dst_key,
                           'observed_count', r.observed,
                           'source_activity_occurrences', r.src_occurrences,
                           'observed_rate', round((r.observed::numeric /
                                                   greatest(r.src_occurrences,1)), 4)),
        r.src_occurrences::int, (r.observed::real / greatest(r.src_occurrences,1))::real);
      n := n + 1;
    END IF;
  END LOOP;

  -- 1b. SoR shows a frequent transition the graph does not model at all.
  FOR r IN
    SELECT ot.src_key, ot.dst_key, ot.observed_count
      FROM sor.observed_transition ot
     WHERE ot.engagement_id = p_engagement
       AND ot.observed_count >= p_min_samples
       AND NOT EXISTS (
         SELECT 1 FROM kg.edge_current e
          WHERE e.engagement_id = p_engagement
            AND e.src_key = ot.src_key AND e.dst_key = ot.dst_key
            AND e.edge_type IN ('precedes','hands_off_to'))
  LOOP
    PERFORM sor.record_drift(
      p_engagement, 'missing_in_graph', 'medium'::sor.drift_severity, 'edge',
      kg.make_edge_key(r.src_key, 'precedes', r.dst_key),
      jsonb_build_object('observed_transition', r.src_key || ' -> ' || r.dst_key,
                         'observed_count', r.observed_count,
                         'suggested_edge_type', 'precedes'),
      r.observed_count::int, NULL::real);
    n := n + 1;
  END LOOP;

  RETURN n;
END $$;

-- Detector 2: control bypass. A `gated_by` control that cases skip.
-- This one escalates to 'critical' because it is a compliance event, not a
-- modelling nicety.
CREATE OR REPLACE FUNCTION sor.detect_control_bypass(
  p_engagement uuid, p_min_samples int DEFAULT 10, p_lookback interval DEFAULT '90 days')
RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int := 0; r record;
BEGIN
  FOR r IN
    WITH gated AS (
      SELECT e.edge_key, e.src_key AS activity_key, e.dst_key AS control_key
        FROM kg.edge_current e
       WHERE e.engagement_id = p_engagement AND e.edge_type = 'gated_by'
    ),
    cases AS (
      SELECT g.edge_key, g.activity_key, g.control_key, o.case_ref,
             bool_or(o2.activity_key IS NOT NULL) AS control_seen
        FROM gated g
        JOIN sor.observation o
          ON o.engagement_id = p_engagement AND o.activity_key = g.activity_key
         AND o.occurred_at > now() - p_lookback
        LEFT JOIN sor.observation o2
          ON o2.engagement_id = p_engagement AND o2.case_ref = o.case_ref
         AND o2.activity_key = g.control_key
         AND o2.occurred_at <= o.occurred_at
       GROUP BY g.edge_key, g.activity_key, g.control_key, o.case_ref
    )
    SELECT edge_key, activity_key, control_key,
           count(*) AS total_cases,
           count(*) FILTER (WHERE NOT control_seen) AS bypassed
      FROM cases
     GROUP BY edge_key, activity_key, control_key
    HAVING count(*) >= p_min_samples AND count(*) FILTER (WHERE NOT control_seen) > 0
  LOOP
    PERFORM sor.record_drift(
      p_engagement, 'control_bypass',
      (CASE WHEN r.bypassed::real / r.total_cases > 0.10 THEN 'critical' ELSE 'high' END)::sor.drift_severity,
      'control', r.control_key,
      jsonb_build_object('gated_activity', r.activity_key,
                         'control', r.control_key,
                         'total_cases', r.total_cases,
                         'bypassed_cases', r.bypassed,
                         'bypass_rate', round((r.bypassed::numeric / r.total_cases), 4)),
      r.total_cases::int, (r.bypassed::real / r.total_cases)::real);
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- Detector 3: actor drift. A different role performs the activity than
-- `performs` asserts.
CREATE OR REPLACE FUNCTION sor.detect_actor_drift(
  p_engagement uuid, p_min_samples int DEFAULT 20, p_lookback interval DEFAULT '90 days')
RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int := 0; r record;
BEGIN
  FOR r IN
    SELECT o.activity_key,
           count(*) AS total,
           count(*) FILTER (
             WHERE NOT EXISTS (
               SELECT 1 FROM kg.edge_current e
                WHERE e.engagement_id = p_engagement AND e.edge_type = 'performs'
                  AND e.src_key = o.actor_role_key AND e.dst_key = o.activity_key)
           ) AS unmodelled,
           (array_agg(DISTINCT o.actor_role_key))[1:5] AS observed_roles
      FROM sor.observation o
     WHERE o.engagement_id = p_engagement
       AND o.activity_key IS NOT NULL AND o.actor_role_key IS NOT NULL
       AND o.occurred_at > now() - p_lookback
     GROUP BY o.activity_key
    HAVING count(*) >= p_min_samples
  LOOP
    IF r.unmodelled::real / r.total > 0.15 THEN
      PERFORM sor.record_drift(
        p_engagement, 'actor_drift', 'medium'::sor.drift_severity, 'node', r.activity_key,
        jsonb_build_object('activity', r.activity_key,
                           'total_observations', r.total,
                           'performed_by_unmodelled_role', r.unmodelled,
                           'observed_roles_sample', r.observed_roles),
        r.total::int, (r.unmodelled::real / r.total)::real);
      n := n + 1;
    END IF;
  END LOOP;
  RETURN n;
END $$;

-- Detector 4: latency drift against a modelled SLA on the edge.
CREATE OR REPLACE FUNCTION sor.detect_latency_drift(
  p_engagement uuid, p_min_samples int DEFAULT 20)
RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int := 0; r record;
BEGIN
  FOR r IN
    SELECT e.edge_key, e.src_key, e.dst_key,
           (e.attributes->>'sla_seconds')::real AS sla,
           ot.p50_gap_seconds, ot.p95_gap_seconds, ot.observed_count
      FROM kg.edge_current e
      JOIN sor.observed_transition ot
        ON ot.engagement_id = e.engagement_id
       AND ot.src_key = e.src_key AND ot.dst_key = e.dst_key
     WHERE e.engagement_id = p_engagement
       AND e.attributes ? 'sla_seconds'
       AND ot.observed_count >= p_min_samples
  LOOP
    IF r.p95_gap_seconds > r.sla THEN
      PERFORM sor.record_drift(
        p_engagement, 'latency_drift',
        (CASE WHEN r.p50_gap_seconds > r.sla THEN 'high' ELSE 'medium' END)::sor.drift_severity,
        'edge', r.edge_key,
        jsonb_build_object('modelled_sla_seconds', r.sla,
                           'observed_p50_seconds', round(r.p50_gap_seconds::numeric, 1),
                           'observed_p95_seconds', round(r.p95_gap_seconds::numeric, 1),
                           'breach_at', CASE WHEN r.p50_gap_seconds > r.sla
                                             THEN 'p50' ELSE 'p95' END),
        r.observed_count::int, (r.p95_gap_seconds / nullif(r.sla,0))::real);
      n := n + 1;
    END IF;
  END LOOP;
  RETURN n;
END $$;

-- Orchestrator. Called by the Workflow Agent's scheduled monitor run.
-- SECURITY DEFINER: REFRESH MATERIALIZED VIEW CONCURRENTLY requires the
-- caller to OWN the matview. The monitor role must not own schema objects,
-- so the refresh runs as the migration owner instead. Without this, drift_scan
-- fails at the refresh and every detector silently runs against stale
-- transition data.
CREATE OR REPLACE FUNCTION sor.run_all_detectors(p_engagement uuid)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = sor, kg, hitl, public AS $$
DECLARE res jsonb;
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY sor.observed_transition;
  SELECT jsonb_build_object(
    'sequence',       sor.detect_sequence_drift(p_engagement),
    'control_bypass', sor.detect_control_bypass(p_engagement),
    'actor',          sor.detect_actor_drift(p_engagement),
    'latency',        sor.detect_latency_drift(p_engagement),
    'ran_at',         now()
  ) INTO res;
  RETURN res;
END $$;

REVOKE ALL ON FUNCTION sor.run_all_detectors(uuid) FROM PUBLIC;
