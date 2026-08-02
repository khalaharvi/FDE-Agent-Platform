-- =====================================================================
-- 009_training.sql
-- Trace capture, labelling, and the rival-grader tournament.
--
-- The whole training strategy rests on one observation: the HITL gates are
-- already producing high-quality human labels as a side effect of doing the
-- work. A merged proposal is a positive label. A rejected one is a negative.
-- An EDITED one is the most valuable label of all -- it is a paired
-- (wrong, right) example on the same input.
--
-- This schema captures traces so that SFT, RL, and grading all read from one
-- consistent store with the KG commit pinned into every row. Without the
-- commit pin, a trace is untrainable: you cannot reconstruct what the model
-- could have seen.
-- =====================================================================

-- ---------------------------------------------------------------------
-- One agent invocation = one trace session. Maps 1:1 to an AgentCore
-- runtimeSessionId, so CloudWatch GenAI Observability spans and these rows
-- join on a single key.
-- ---------------------------------------------------------------------
CREATE TABLE trn.trace_session (
  session_id       uuid PRIMARY KEY,          -- == AgentCore runtimeSessionId
  engagement_id    uuid NOT NULL,
  agent_name       text NOT NULL CHECK (agent_name IN ('engagement','workflow','development')),
  agent_runtime_arn text NOT NULL,
  agent_qualifier  text,                      -- endpoint/version, for A/B attribution
  model_id         text NOT NULL,
  policy_version   text,                      -- our own tag: base | sft-v3 | grpo-v2
  -- The graph state the agent could see. Non-negotiable for reproducibility.
  base_commit_id   bigint NOT NULL REFERENCES kg.commit(commit_id),
  task_kind        text NOT NULL,             -- 'map_workflow' | 'author_workflow' | 'triage_drift' | ...
  task_input       jsonb NOT NULL,
  final_output     jsonb,
  outcome          trn.trace_outcome NOT NULL DEFAULT 'pending',
  -- Populated when the human gate closes. THIS is the training label.
  label_proposal_id bigint REFERENCES hitl.proposal(proposal_id),
  label_source     text CHECK (label_source IN ('hitl_gate','eval_harness','operator_feedback')),
  split            trn.split,
  total_tokens     int,
  latency_ms       int,
  started_at       timestamptz NOT NULL DEFAULT now(),
  ended_at         timestamptz
);
CREATE INDEX trace_session_label_idx ON trn.trace_session (outcome, agent_name, split);
CREATE INDEX trace_session_engagement_idx ON trn.trace_session (engagement_id, started_at DESC);

-- ---------------------------------------------------------------------
-- Every step of the trajectory. This is the SFT/RL training substrate.
--
-- role/content/tool_calls mirror the OpenAI messages schema exactly, because
-- that is what TRL's SFTTrainer + chat templates and verl's multi-turn
-- rollout both consume. Storing it in any other shape means writing a
-- converter, and converters are where loss-masking bugs live.
-- ---------------------------------------------------------------------
CREATE TABLE trn.trace_step (
  step_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id     uuid NOT NULL REFERENCES trn.trace_session(session_id) ON DELETE CASCADE,
  turn           int  NOT NULL,
  role           text NOT NULL CHECK (role IN ('system','user','assistant','tool')),
  content        text,
  -- assistant turns: the tool calls emitted, OpenAI shape.
  tool_calls     jsonb,
  -- tool turns: which call this answers, and the result.
  tool_call_id   text,
  tool_name      text,
  tool_result    jsonb,
  -- CRITICAL FOR TRAINING. false on every 'tool' and 'user' turn.
  -- The SFT collator and the RL rollout both key loss masking off this column.
  -- Training on retrieved tokens is the single most common cause of a
  -- retrieval-trained model that hallucinates plausible-looking evidence
  -- instead of calling the tool.
  trainable      boolean NOT NULL,
  -- Retrieval telemetry, populated for kg.* tool calls. Feeds the RL reward.
  retrieval      jsonb,   -- {"k":20,"returned":18,"rrf_top":0.031,
                          --  "lists_matched":{"node_ann":12,"graph_expand":9},
                          --  "hops":2,"nodes_visited":143,"latency_ms":38}
  -- Did the model's next assertion actually appear in what it retrieved?
  -- Computed post-hoc by the grounding checker; NULL until then.
  grounded       boolean,
  grounding_detail jsonb,
  latency_ms     int,
  tokens_in      int,
  tokens_out     int,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, turn)
);
CREATE INDEX trace_step_session_idx ON trn.trace_step (session_id, turn);
CREATE INDEX trace_step_tool_idx    ON trn.trace_step (tool_name) WHERE tool_name IS NOT NULL;
CREATE INDEX trace_step_ungrounded_idx
  ON trn.trace_step (session_id) WHERE grounded = false;

-- ---------------------------------------------------------------------
-- Traversal failures -- the specific thing RL is pointed at.
--
-- Categorising failures explicitly (rather than lumping them into "wrong
-- answer") is what lets the reward function target them. Each kind maps to
-- a distinct reward term in training/grpo_rewards.py.
-- ---------------------------------------------------------------------
CREATE TYPE trn.traversal_failure AS ENUM (
  'empty_result',        -- query returned nothing; usually a filter/type mistake
  'wrong_entry_point',   -- seeded on the wrong node, everything downstream is off
  'under_retrieval',     -- stopped too early; the answer was 1 more hop away
  'over_retrieval',      -- pulled 400 nodes to answer a 2-node question
  'wrong_edge_type',     -- followed `precedes` when the question needed `depends_on`
  'schema_invalid',      -- called a tool with arguments the schema rejects
  'cycle_thrash',        -- re-visited the same subgraph repeatedly
  'ungrounded_claim',    -- asserted something not present in any retrieved row
  'stale_commit',        -- reasoned over a commit older than head without saying so
  'hop_budget_exceeded'  -- hit max_hops/max_nodes and gave up
);

CREATE TABLE trn.failure_label (
  failure_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id   uuid NOT NULL REFERENCES trn.trace_session(session_id) ON DELETE CASCADE,
  step_id      bigint REFERENCES trn.trace_step(step_id) ON DELETE CASCADE,
  kind         trn.traversal_failure NOT NULL,
  -- 'auto' failures come from deterministic checks in the eval harness;
  -- 'human' failures come from a reviewer marking up a trace. Both are used,
  -- but only 'human' ones count toward the calibration set.
  labeled_by   text NOT NULL CHECK (labeled_by IN ('auto','human','judge')),
  detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- What the correct action would have been. Populated for the highest-value
  -- subset; these become the RL curriculum's hard cases.
  corrective_action jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX failure_label_kind_idx ON trn.failure_label (kind, created_at DESC);
CREATE INDEX failure_label_session_idx ON trn.failure_label (session_id);

-- ---------------------------------------------------------------------
-- RIVAL GRADERS
--
-- Two retrieval configurations answer the same question. A judge sees both
-- (order randomised) and picks a winner. Aggregated with Bradley-Terry.
--
-- Pairwise beats pointwise for judge reliability -- absolute 1-10 scoring
-- drifts badly across sessions while relative preference is stable. Position
-- bias is real and is worst exactly when the two candidates are close, which
-- is the regime that matters here; hence every pair is run in BOTH orders
-- and disagreements are recorded rather than silently averaged away.
-- ---------------------------------------------------------------------
CREATE TABLE trn.retriever_variant (
  variant_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name         text NOT NULL UNIQUE,       -- 'rrf-k60-2hop', 'ann-only', 'graph-heavy'
  description  text,
  -- Exact kg.hybrid_search arguments. Reproducible by construction.
  config       jsonb NOT NULL,
  is_champion  boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX retriever_one_champion ON trn.retriever_variant (is_champion) WHERE is_champion;

CREATE TABLE trn.eval_query (
  query_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id uuid NOT NULL,
  question     text NOT NULL,
  -- Pooled relevance judgements, TREC-style: the union of what every variant
  -- has ever returned for this query, judged once, reused across all
  -- comparisons. Avoids the "unjudged == irrelevant" bias that makes a new
  -- variant look worse than it is.
  pooled_keys  text[] NOT NULL DEFAULT '{}',
  relevant_keys text[] NOT NULL DEFAULT '{}',
  judged_by    text,
  judged_at    timestamptz,
  difficulty   text CHECK (difficulty IN ('easy','medium','hard')),
  hops_required int,
  split        trn.split NOT NULL DEFAULT 'test',
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trn.duel (
  duel_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  query_id     bigint NOT NULL REFERENCES trn.eval_query(query_id),
  variant_a    bigint NOT NULL REFERENCES trn.retriever_variant(variant_id),
  variant_b    bigint NOT NULL REFERENCES trn.retriever_variant(variant_id),
  -- Which one was shown FIRST to the judge. Required to measure position bias.
  presented_first bigint NOT NULL REFERENCES trn.retriever_variant(variant_id),
  judge_model  text NOT NULL,
  judge_prompt_version text NOT NULL,
  winner       bigint REFERENCES trn.retriever_variant(variant_id),  -- NULL = tie
  confidence   real CHECK (confidence BETWEEN 0 AND 1),
  rationale    text,
  -- The paired reversed-order duel. Set on both rows. If the two disagree,
  -- the pair is position-biased and both are excluded from the BT fit.
  mirror_duel_id bigint REFERENCES trn.duel(duel_id),
  consistent   boolean,
  -- Human adjudication on a sampled subset, for judge calibration.
  human_winner bigint REFERENCES trn.retriever_variant(variant_id),
  human_judged_by text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT duel_distinct_variants CHECK (variant_a <> variant_b)
);
CREATE INDEX duel_query_idx ON trn.duel (query_id);
CREATE INDEX duel_calibration_idx ON trn.duel (judge_model) WHERE human_winner IS NOT NULL;

-- Bradley-Terry via simple iterative MM. Kept in SQL so the leaderboard is
-- always live and no one has to run a notebook to see which retriever wins.
-- Only order-consistent duels are fitted.
CREATE FUNCTION trn.bradley_terry(p_iterations int DEFAULT 100)
RETURNS TABLE (variant_id bigint, name text, strength real, elo real,
               wins int, losses int, ties int)
-- VOLATILE, not STABLE: the MM fit materialises intermediate state in a temp
-- table, which Postgres forbids in a non-volatile function.
LANGUAGE plpgsql AS $$
DECLARE i int;
BEGIN
  CREATE TEMP TABLE IF NOT EXISTS _bt (vid bigint PRIMARY KEY, p double precision)
    ON COMMIT DROP;
  DELETE FROM _bt;
  INSERT INTO _bt SELECT rv.variant_id, 1.0 FROM trn.retriever_variant rv;

  FOR i IN 1..p_iterations LOOP
    WITH pairs AS (
      SELECT d.variant_a AS x, d.variant_b AS y,
             count(*) FILTER (WHERE d.winner = d.variant_a) AS wx,
             count(*) FILTER (WHERE d.winner = d.variant_b) AS wy,
             count(*) AS n
        FROM trn.duel d
       WHERE d.winner IS NOT NULL AND coalesce(d.consistent, true)
       GROUP BY d.variant_a, d.variant_b
    ),
    sym AS (
      SELECT x AS vid, wx AS w, n, y AS opp FROM pairs
      UNION ALL
      SELECT y, wy, n, x FROM pairs
    ),
    upd AS (
      SELECT s.vid,
             sum(s.w)::double precision AS wins,
             sum(s.n / (bx.p + by.p)) AS denom
        FROM sym s
        JOIN _bt bx ON bx.vid = s.vid
        JOIN _bt by ON by.vid = s.opp
       GROUP BY s.vid
    )
    UPDATE _bt b
       SET p = greatest(upd.wins / nullif(upd.denom, 0), 1e-9)
      FROM upd WHERE upd.vid = b.vid AND upd.denom > 0;

    -- normalise to keep the fit from drifting
    UPDATE _bt SET p = p / (SELECT avg(p) FROM _bt);
  END LOOP;

  RETURN QUERY
  SELECT rv.variant_id, rv.name, b.p::real,
         (1500 + 400 * ln(greatest(b.p, 1e-9)) / ln(10))::real,
         (SELECT count(*)::int FROM trn.duel d
           WHERE d.winner = rv.variant_id AND coalesce(d.consistent,true)),
         (SELECT count(*)::int FROM trn.duel d
           WHERE rv.variant_id IN (d.variant_a, d.variant_b)
             AND d.winner IS NOT NULL AND d.winner <> rv.variant_id
             AND coalesce(d.consistent,true)),
         (SELECT count(*)::int FROM trn.duel d
           WHERE rv.variant_id IN (d.variant_a, d.variant_b) AND d.winner IS NULL)
    FROM trn.retriever_variant rv JOIN _bt b ON b.vid = rv.variant_id
   ORDER BY b.p DESC;
END $$;

-- Judge calibration against human adjudication. Target band is Cohen's
-- kappa ~0.78-0.82, which is where human-to-human agreement sits. A judge
-- scoring much ABOVE that band is not better -- it is usually overfit to a
-- prompt artefact or the eval set is too easy. Investigate either extreme.
CREATE FUNCTION trn.judge_kappa(p_judge_model text)
RETURNS TABLE (n int, observed_agreement real, expected_agreement real,
               cohens_kappa real, verdict text)
LANGUAGE sql STABLE AS $$
  WITH j AS (
    SELECT (winner IS NOT DISTINCT FROM human_winner) AS agree,
           winner, human_winner
      FROM trn.duel
     WHERE judge_model = p_judge_model AND human_winner IS NOT NULL
       AND coalesce(consistent, true)
  ),
  n AS (SELECT count(*)::real AS total FROM j),
  po AS (SELECT count(*) FILTER (WHERE agree)::real / nullif((SELECT total FROM n),0) AS v FROM j),
  -- expected agreement under independence of the two raters' marginals
  pe AS (
    SELECT sum(pj * ph) AS v
      FROM (
        SELECT coalesce(winner, -1) AS w,
               count(*)::real / nullif((SELECT total FROM n),0) AS pj
          FROM j GROUP BY 1
      ) a
      FULL JOIN (
        SELECT coalesce(human_winner, -1) AS w,
               count(*)::real / nullif((SELECT total FROM n),0) AS ph
          FROM j GROUP BY 1
      ) b USING (w)
  )
  SELECT (SELECT total FROM n)::int,
         (SELECT v FROM po)::real,
         coalesce((SELECT v FROM pe), 0)::real,
         (((SELECT v FROM po) - coalesce((SELECT v FROM pe),0))
          / nullif(1 - coalesce((SELECT v FROM pe),0), 0))::real,
         CASE
           WHEN (SELECT total FROM n) < 50 THEN 'insufficient_sample'
           WHEN (((SELECT v FROM po) - coalesce((SELECT v FROM pe),0))
                 / nullif(1 - coalesce((SELECT v FROM pe),0),0)) < 0.60
             THEN 'unusable: judge disagrees with humans too often'
           WHEN (((SELECT v FROM po) - coalesce((SELECT v FROM pe),0))
                 / nullif(1 - coalesce((SELECT v FROM pe),0),0)) < 0.78
             THEN 'marginal: usable for ranking, not for RL reward'
           WHEN (((SELECT v FROM po) - coalesce((SELECT v FROM pe),0))
                 / nullif(1 - coalesce((SELECT v FROM pe),0),0)) <= 0.82
             THEN 'calibrated: human-equivalent'
           ELSE 'suspiciously high: check for prompt artefact or trivial eval set'
         END;
$$;

-- Position-bias rate for a judge. Any mirrored pair where the judge picked
-- whichever option was shown first is a bias event.
CREATE FUNCTION trn.judge_position_bias(p_judge_model text)
RETURNS TABLE (mirrored_pairs int, inconsistent int, bias_rate real,
               first_position_win_rate real)
LANGUAGE sql STABLE AS $$
  SELECT count(*)::int / 2,
         (count(*) FILTER (WHERE NOT consistent))::int / 2,
         (count(*) FILTER (WHERE NOT consistent))::real / nullif(count(*),0),
         (count(*) FILTER (WHERE winner = presented_first))::real / nullif(count(*),0)
    FROM trn.duel
   WHERE judge_model = p_judge_model AND mirror_duel_id IS NOT NULL;
$$;

-- ---------------------------------------------------------------------
-- Export view for SFT. One row per trainable trajectory, already in the
-- messages shape. `trainable` is carried per message so the collator can
-- build the label mask without re-deriving it.
-- ---------------------------------------------------------------------
CREATE VIEW trn.sft_export AS
SELECT s.session_id,
       s.agent_name,
       s.task_kind,
       s.split,
       s.base_commit_id,
       jsonb_agg(
         jsonb_build_object(
           'role', st.role,
           'content', st.content,
           'tool_calls', st.tool_calls,
           'tool_call_id', st.tool_call_id,
           'name', st.tool_name,
           'trainable', st.trainable
         ) ORDER BY st.turn
       ) AS messages,
       count(*) FILTER (WHERE st.role = 'assistant') AS assistant_turns,
       count(*) FILTER (WHERE st.tool_name IS NOT NULL) AS tool_calls,
       bool_and(coalesce(st.grounded, true)) AS fully_grounded
  FROM trn.trace_session s
  JOIN trn.trace_step st ON st.session_id = s.session_id
 WHERE s.outcome IN ('accepted','corrected')
 GROUP BY s.session_id, s.agent_name, s.task_kind, s.split, s.base_commit_id;

COMMENT ON VIEW trn.sft_export IS
  'SFT-ready trajectories. Only accepted/corrected sessions. Each message '
  'carries `trainable`; the collator must set labels=-100 wherever it is '
  'false (all tool and user turns).';
