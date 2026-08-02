# Drift Monitor — SoR Adapters and Detection

The graph is a model of how work is done. The systems of record (SoR) show
how work is *actually* done, right now. This document covers the adapter
contract that connects the two, the four deterministic detectors, and the
operational rules around dedup, severity, and escalation. Everything below
matches `db/007_drift.sql` and `db/tests/smoke_test.sql`'s TEST 12/13
exactly.

---

## 1. Two axes, genuinely different problems

| Axis | What diverges | Cause | Resolution |
|---|---|---|---|
| **Reality drift** | The graph asserts how work is done; the SoR shows something else | The graph is stale, or the business changed | `hitl.proposal` — change the graph |
| **Pin drift** | A published workflow was authored against commit N; the graph is now at N+k and something bound has changed underneath it | Normal graph evolution outpacing a workflow's authoring | Re-author the workflow against head |

Both land in `sor.drift_signal` (`sor.drift_kind` covers both:
`missing_in_sor`, `missing_in_graph`, `sequence_drift`, `actor_drift`,
`latency_drift`, `volume_drift`, `control_bypass` are reality drift;
`stale_pin` is pin drift). Pin drift is raised transactionally, inside
`hitl.merge_proposal` itself (`db/005`), for every published workflow pinned
behind the new head — nobody has to remember to check, and it cannot be
missed the way a scheduled scan theoretically could be.

---

## 2. The SoR adapter contract

`sor.adapter` (`db/007`) registers one adapter per external system:

```sql
CREATE TABLE sor.adapter (
  adapter_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  engagement_id   uuid NOT NULL,
  adapter_key     text NOT NULL,          -- 'jira-prod', 'sfdc-cpq', 'anaplan'
  system_node_key text NOT NULL,          -- the kg 'system' node this adapter observes
  kind            text NOT NULL CHECK (kind IN
                    ('rest_poll','webhook','event_stream','db_cdc','warehouse_query')),
  secret_arn      text,                   -- connection lives in Secrets Manager
  mapping         jsonb NOT NULL,         -- the contract below
  poll_cron       text,
  is_active       boolean NOT NULL DEFAULT true,
  last_synced_at  timestamptz,
  last_cursor     text,
  ...
);
```

`mapping` is the entire adapter-specific contract, and it is deliberately
generic — every adapter, regardless of source system, emits rows into the
same `sor.observation` table. An adapter's only job is: given a raw record
from its system, produce `(case_ref, activity_key, raw_activity, actor_hash,
actor_role_key, occurred_at, duration_seconds)`.

### 2.1 Worked example: Jira

```json
{
  "activity_field": "status",
  "activity_map": {
    "In Review": "act.legal_review",
    "Deal Desk Approved": "act.discount_review",
    "Closed": "act.close_quote"
  },
  "actor_field": "assignee.accountId",
  "actor_role_map": {
    "5b10a2844c20165700ede21g": "role.deal_desk",
    "5b10ac8d82e05b22cc7d4ef5": "role.legal"
  },
  "timestamp_field": "updated",
  "case_id_field": "key",
  "duration_field": null
}
```

`activity_map` translates Jira's free-text `status` values onto graph
`activity_key`s — a status with no entry means `activity_key` comes back
`NULL`, which is itself a `missing_in_graph` signal (Section 5, Detector 1)
rather than a silently dropped observation. `actor_role_map` resolves Jira
account IDs to graph `role_key`s; an unmapped assignee produces a `NULL`
`actor_role_key`, which the actor-drift detector cannot use (it requires
`actor_role_key IS NOT NULL`) — an unmapped-actor observation is invisible to
drift detection until someone adds it to the map, which is the correct
failure mode (silence, not a wrong-role false positive).

### 2.2 Worked example: Salesforce

```json
{
  "activity_field": "StageName",
  "activity_map": {
    "Proposal/Price Quote": "act.create_quote",
    "Negotiation/Review": "act.discount_review",
    "Closed Won": "act.close_quote"
  },
  "actor_field": "OwnerId",
  "actor_role_map": {
    "005Dn000001abCDEFG": "role.sales_rep",
    "005Dn000001hijKLMN": "role.deal_desk"
  },
  "timestamp_field": "LastModifiedDate",
  "case_id_field": "Id",
  "duration_field": null
}
```

Salesforce opportunity stages map onto activities the same way Jira statuses
do — the adapter's `kind='rest_poll'` here (SOQL query on a `poll_cron`
schedule against `LastModifiedDate > last_cursor`), versus Jira's `kind`
could be `webhook` if the deployment wires up Jira's native webhooks instead
of polling. The mapping shape does not care which.

### 2.3 Worked example: ServiceNow

```json
{
  "activity_field": "state",
  "activity_map": {
    "2": "act.triage",
    "3": "act.legal_review",
    "6": "act.close_quote"
  },
  "actor_field": "assigned_to.value",
  "actor_role_map": {
    "a8f8e7b1db1e12102...": "role.legal"
  },
  "timestamp_field": "sys_updated_on",
  "case_id_field": "number",
  "duration_field": "business_duration"
}
```

ServiceNow's numeric `state` codes are exactly the kind of case the mapping
exists for — the raw value is meaningless outside the ITSM instance's own
configuration, and hard-coding "state 3 means legal review" into detector SQL
would make the detectors non-portable across customers. `duration_field`
here is populated (ServiceNow tracks business-hours duration natively);
when present, the adapter can populate `sor.observation.duration_seconds`
directly instead of relying on `sor.observed_transition`'s computed gap
between consecutive observations.

### 2.4 Worked example: a generic event stream

```json
{
  "activity_field": "event_type",
  "activity_map": {
    "quote.discount_review.completed": "act.discount_review",
    "quote.legal.completed": "act.legal_review"
  },
  "actor_field": "actor.employee_id",
  "actor_role_map": {
    "EMP-4471": "role.deal_desk"
  },
  "timestamp_field": "event_time",
  "case_id_field": "quote_id",
  "duration_field": "duration_ms"
}
```

An `event_stream` adapter (Kinesis/EventBridge) is the same contract as a
`rest_poll` adapter — the `kind` column only tells the platform how the
adapter is invoked (a scheduled poll vs. a subscription), not how its
`mapping` is interpreted. This is intentional: adding a fifth SoR kind later
(a `db_cdc` adapter reading a customer's own operational Postgres, say) needs
no change to `sor.observation`'s shape or to any detector — only a new
`kind` value and an adapter implementation that produces the same normalised
row shape.

### 2.5 Why the mapping is authored by a human, never inferred by an agent

The mapping *is* the measurement instrument. If an agent inferred
`activity_map` from a sample of Jira tickets, two failure modes follow
directly:

- **The instrument would be wrong in the same direction the graph is wrong.**
  An LLM guessing that Jira's "In Review" means whatever the graph currently
  calls "legal review" is reasoning from the same prior the graph itself
  encodes — if the graph is stale or wrong about what "In Review" means in
  practice, an inferred mapping inherits that error and then "confirms" it,
  because the detector would compare the graph against a mapping derived
  from the graph. Drift detection against a self-referential measurement
  instrument cannot detect the thing it exists to detect.
- **A wrong mapping is worse than no mapping**, because it produces
  confident, specific, wrong signals — a `control_bypass` finding that fires
  because "Deal Desk Approved" was mapped to the wrong activity is not a
  false positive that a human can shrug off; it's a compliance-adjacent claim
  that consumes a reviewer's trust in the queue for nothing.

The mapping is authored once, by a human, at onboarding, from direct
knowledge of what the SoR's fields actually mean — the same category of fact
an SME provides for the graph itself, and for the same reason: it's a claim
about the business that needs a person who knows the business to make it.

---

## 3. `sor.observation` normalisation and the actor_hash PII posture

```sql
CREATE TABLE sor.observation (
  ...
  case_ref        text   NOT NULL,
  activity_key    text,                 -- NULL = SoR showed something ungraphed
  raw_activity    text   NOT NULL,      -- always kept, even when activity_key is NULL
  actor_hash      text,                 -- opaque, salted hash. NEVER a name, NEVER an email.
  actor_role_key  text,                 -- resolved role, when derivable
  system_object_key text,
  occurred_at     timestamptz NOT NULL,
  duration_seconds int,
  attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,
  ...
);
```

`raw_activity` is always kept regardless of whether `activity_map` resolved
it — this is what lets Detector 1's `missing_in_graph` branch (Section 5)
name the actual unmapped SoR value in its `detail`, rather than reporting "an
unknown activity occurred" with no way for a reviewer to act on it.

`actor_hash` is a salted, one-way hash of whatever identity field the adapter
observed (an email, an account ID, an employee ID) — computed by the
adapter before the row ever reaches `sor.observation`, never a name, never
an email, never reversible without the salt. This mirrors the graph's own
PII posture (`docs/02-agent-engagement.md`: "the graph models roles, not
people") extended to observational data: `actor_role_key` is what detectors
and reviewers actually reason about (a *role*, mapped once at onboarding via
`actor_role_map`), and `actor_hash` exists only so that two observations by
the same unmapped individual can be recognised as the same individual
without ever recording who that individual is. It is a tripwire, not a
guarantee — the same caveat `docs/02`'s `node_role_is_not_a_person` check
carries: pair it with review of what adapters actually populate `actor_hash`
with at onboarding time, since the hash is only as private as the field it's
salted from and the salt's own handling.

---

## 4. `sor.observed_transition`, `CONCURRENTLY`, and the ownership issue

```sql
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
SELECT engagement_id, activity_key AS src_key, next_activity AS dst_key,
       count(*) AS observed_count,
       avg(...) AS avg_gap_seconds,
       percentile_cont(0.5)  WITHIN GROUP (...) AS p50_gap_seconds,
       percentile_cont(0.95) WITHIN GROUP (...) AS p95_gap_seconds,
       min(occurred_at) AS first_seen, max(occurred_at) AS last_seen
  FROM ordered WHERE next_activity IS NOT NULL
 GROUP BY engagement_id, activity_key, next_activity;
```

This is a materialized view, not a live query, because every detector needs
the same derived transitions (consecutive-observation pairs per case), and
computing the `lead()` window over potentially millions of observation rows
once per scan — rather than once per detector — is the difference between a
scan that touches the observation table once and one that touches it four
times.

`sor.run_all_detectors` refreshes it `CONCURRENTLY`:

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY sor.observed_transition;
```

`CONCURRENTLY` matters because a plain `REFRESH MATERIALIZED VIEW` takes an
`ACCESS EXCLUSIVE` lock, blocking any concurrent read of the view for the
refresh's full duration — on a busy engagement with adapters actively
writing observations, that would mean any in-flight `kg_search`/drift query
touching the same connection pool stalls behind the refresh. `CONCURRENTLY`
instead builds a new version alongside the old and swaps atomically,
requiring only a `UNIQUE` index on the view (`observed_transition_uq` on
`(engagement_id, src_key, dst_key)`) — without that index, `REFRESH
CONCURRENTLY` itself fails.

**The ownership issue, precisely:** `REFRESH MATERIALIZED VIEW CONCURRENTLY`
requires the *calling role* to own the matview — there is no grantable
`REFRESH` privilege on PostgreSQL 16 (pre-PG17) short of ownership. `sor.
run_all_detectors` is `SECURITY DEFINER`, owned by the migration owner, for
exactly this reason: it lets a narrowly-privileged role (`fde_ingest`, or
`fde_agent` once granted `EXECUTE`) trigger the refresh without needing to
own the object itself. Verified live against the deployed schema (pgvector
0.8.2 / PG 16.14): calling `sor.run_all_detectors` as `fde_ingest` succeeds
and refreshes the view correctly. See `docs/05-mcp-surface.md` Section 2.3
for the separate, currently-real gap this platform has today: `fde_agent`
(the role the MCP server's `drift_scan` tool actually runs as) has no
`EXECUTE` grant on this function at all, so `drift_scan` fails before it ever
reaches the refresh — a missing grant, not a `SECURITY DEFINER` design flaw.

---

## 5. The four detectors

All four are pure SQL functions in `db/007_drift.sql`, called by
`sor.run_all_detectors`. None of them make a judgement call — they compute a
rate against a threshold and a minimum sample size, and call
`sor.record_drift` if the threshold is crossed.

| Detector | Compares | Fires when | Severity |
|---|---|---|---|
| `detect_sequence_drift` | `precedes`/`hands_off_to` edges vs. `sor.observed_transition` | (a) a modelled transition is observed in < 5% of that activity's occurrences, with ≥20 source-activity occurrences; or (b) an unmodelled transition is observed ≥20 times | `high` if observed=0, else `medium` (case a); `medium` (case b) |
| `detect_control_bypass` | `gated_by` edges vs. per-case observation of the control activity | any case reaches the gated activity's downstream without the control activity appearing first, with ≥10 total cases for that control | `critical` if bypass rate > 10%, else `high` |
| `detect_actor_drift` | `performs` edges vs. `sor.observation.actor_role_key` | > 15% of an activity's occurrences performed by a role with no matching `performs` edge, with ≥20 occurrences | `medium` |
| `detect_latency_drift` | `edge.attributes.sla_seconds` vs. `sor.observed_transition`'s p50/p95 gap | observed p95 gap exceeds the modelled SLA, with ≥20 observed transitions | `high` if p50 also exceeds SLA, else `medium` |

### 5.1 What a firing means, in plain SQL logic

**Sequence drift (missing_in_sor):** for every live `precedes`/`hands_off_to`
edge, count how many times the source activity was observed at all in the
lookback window, and how many of those transitioned to the modelled
destination per `sor.observed_transition`. If the source activity has enough
volume to judge (`≥20`) and the modelled transition happened for less than 5%
of them, the graph is asserting a path the business barely takes.

**Sequence drift (missing_in_graph):** the reverse — an observed transition
with `observed_count ≥ 20` and no corresponding `precedes`/`hands_off_to`
edge in the graph at all. The business is doing something the graph never
learned about.

**Control bypass:** for every `gated_by` edge (`activity → control`), look at
every case that reached the gated activity, and check whether the control
activity was observed on that same case *before* the gated activity occurred.
Cases where it wasn't are bypasses. Requires ≥10 total cases to judge; above
10% bypass rate is `critical` (a genuine compliance-severity finding), below
that is `high` (compliance-relevant but not yet alarming volume).

**Actor drift:** for every activity with ≥20 role-attributable observations,
what fraction were performed by a role with no `performs` edge to that
activity in the graph. Above 15%, the graph's picture of who does the work is
wrong often enough to matter.

**Latency drift:** for every edge carrying `attributes.sla_seconds`, compare
the modelled SLA against the *observed* p50/p95 gap between the two
activities (from `sor.observed_transition`, requiring ≥20 observed
transitions). A p95 breach alone is `medium` — some cases are slow. A p50
breach means the *typical* case now exceeds the modelled SLA, which is
`high`.

### 5.2 False-positive modes, per detector

| Detector | False-positive mode |
|---|---|
| `detect_sequence_drift` | An adapter mapping gap (Section 2) makes an activity look unobserved when it's actually just unmapped — check `raw_activity` values behind a `missing_in_graph` signal before assuming it's a real gap |
| `detect_control_bypass` | The control's *own* activity is itself poorly mapped (Section 2.1's silent-drop failure mode) — a control that's genuinely happening but invisible to the adapter looks identical to a genuine bypass |
| `detect_actor_drift` | A recent, deliberate reorg moved responsibility to a new role and the graph hasn't been updated yet — this is real reality drift, correctly detected, but the "fix" is a graph update, not alarm |
| `detect_latency_drift` | A modelled SLA that was aspirational rather than measured to begin with — the detector cannot distinguish "the process got slower" from "the SLA was never realistic" |

---

## 6. Dedup: `drift_signal_dedup_uq` and why a noisy queue is worse than none

```sql
CREATE UNIQUE INDEX drift_signal_dedup_uq
  ON sor.drift_signal (engagement_id, drift_kind, subject_kind, subject_ref)
  WHERE state IN ('open','triaged','proposal_raised');
```

`sor.record_drift`'s `INSERT ... ON CONFLICT (...) WHERE state IN
(...) DO UPDATE` upserts against this index: re-detecting the same drift
(same engagement, kind, subject) while it's still in a non-terminal state
updates `last_seen_at`, increments `occurrences`, and takes the `greatest()`
of old and new severity — it never inserts a second row. `smoke_test.sql`'s
TEST 13 asserts this directly: running `detect_control_bypass` three times
against the same bypass produces one signal with `occurrences = 3`, not
three signals.

The design bet here is explicit, and stated in `docs/03-agent-workflow.md`:
"a noisy queue gets ignored, and an ignored queue is worse than no queue
because it creates the appearance of monitoring." A detector that fires fresh
on every 6-hour scan for a condition that hasn't changed trains reviewers to
skim past drift signals as a category, which is a worse outcome than having
no automated drift detection at all — the false confidence of "someone would
notice" without anyone actually noticing. `occurrences` makes recurrence
*visible* (a signal seen 40 times across ten days is a different priority
than one seen twice) without making it *noisy*.

---

## 7. Severity model and escalation

`sor.drift_severity`: `info < low < medium < high < critical`, an ordered
enum (comparable directly, as `drift_list`'s `severity >= min_severity`
filter relies on).

| Severity | Meaning | Path |
|---|---|---|
| `critical` | `control_bypass` above 10% — compliance-severity | Immediate SNS to the compliance channel from the scheduled monitor run, in addition to landing in the queue |
| `high` | Compliance-relevant bypass below 10%, zero-observation sequence gaps, p50 SLA breaches | Standard queue, prioritised first |
| `medium` | Most detector output — real but not urgent | Standard queue |
| `low` | `stale_pin` on a merge (informational by default) | Standard queue, typically batched with routine re-authoring |
| `info` | Reserved for detectors that want to record something without asking anyone to act | Not currently emitted by any shipped detector |

### The control_bypass distinction

A `control_bypass` firing means one of two very different things, and
conflating them is the single most consequential triage mistake this system
can make:

1. **The graph is wrong** — the control isn't actually mandatory in every
   case, or applies only under conditions the graph doesn't model (e.g. it
   only applies above a discount threshold the graph never encoded as a
   branch condition). Fix: propose an `update_edge` adding the missing
   condition to the `gated_by` edge's `attributes`.
2. **The graph is right, and the control is being bypassed in practice** — a
   genuine compliance finding, independent of anything about the graph's
   accuracy.

The Workflow Agent's triage rule (`docs/03-agent-workflow.md`) is explicit
that it must state which one, with evidence, and must not default to (1)
merely because (1) is the one it has a tool to fix — "proposing a graph
change to make a bypass signal disappear is the worst thing you can do in
this role." The same discipline applies to a human reviewing the queue
directly (`docs/10-prodops-runbook.md`): the question to ask first is never
"how do we make this signal go away," it's "which of these two things is
actually true."

---

## 8. Adding a new detector

A new detector must satisfy the same contract every shipped detector does:

1. **Deterministic.** Pure SQL over `kg.edge_current`/`sor.observation`/
   `sor.observed_transition` — no model call inside the detector itself. Two
   people (or two scan runs) must get the identical answer from the identical
   data.
2. **Minimum sample size, stated explicitly in the function signature** (as a
   parameter with a default, the way `p_min_samples` appears in all four
   shipped detectors) — never fire on n too small to mean anything. An n=3
   coincidence reaching a human is exactly the failure `docs/00-architecture.md`
   calls out as corrosive to queue trust.
3. **Dedups on a natural key** via `sor.record_drift` — never `INSERT`
   directly into `sor.drift_signal`. The natural key is
   `(engagement_id, drift_kind, subject_kind, subject_ref)`; if the new
   detector's notion of "the same drift recurring" doesn't fit that four-part
   key, that's a sign either the new `drift_kind` needs its own dedicated
   `subject_kind`, or the detector is conflating two different findings under
   one signal.
4. **Writes via `sor.record_drift` exclusively**, never a hand-rolled
   `INSERT`/`ON CONFLICT` — this is what keeps `occurrences`, `last_seen_at`,
   and the severity-`greatest()` merge behaviour consistent across every
   detector, present and future, without each one reimplementing it.
5. Register it in `sor.run_all_detectors`'s `jsonb_build_object` so the
   scheduled scan actually runs it, and add its name to the summary that
   `drift_scan` reports.
6. If it introduces a new `sor.drift_kind` value, that's an `ALTER TYPE ...
   ADD VALUE` migration (same rule as `docs/01-knowledge-graph.md` Section
   9.2 for graph types) reviewed by a human, and the Workflow Agent's triage
   prompt needs an explicit rule for the new kind — an un-enumerated
   `drift_kind` reaching triage with no stated rule is exactly the ambiguity
   the `control_bypass` distinction (Section 7) exists to prevent recurring.

---

## 9. Scheduling

The shipped shape (`docs/03-agent-workflow.md`): EventBridge on a **6-hour**
rate, invoking a Lambda that calls `invoke_agent_runtime(fde_workflow,
{"task": "monitor_drift", ...})` per active engagement, which calls
`drift_scan` → triages → drafts (never submits) proposals for anything above
threshold.

**Cadence guidance:** 6 hours balances two costs. Too frequent, and the scan
cost (a materialized view refresh plus four detector passes over
`sor.observation`, which grows without bound over an engagement's life) adds
up against a Lambda/Aurora bill for marginal freshness gain — drift that
matters (a control bypass, a wrong-role pattern) does not typically resolve
or worsen meaningfully inside a few hours. Too infrequent, and a
`critical`-severity `control_bypass` sits undetected for longer than a
compliance process should tolerate; 6 hours keeps the worst-case detection
lag inside one business day even across a weekend gap in monitoring runs.

**Cost shape:** the dominant cost is Aurora compute during the scan (the
`REFRESH MATERIALIZED VIEW CONCURRENTLY` plus four sequential detector
queries), not the EventBridge/Lambda invocation itself, which is priced
per-invocation and negligible at a 6-hourly cadence. As `sor.observation`
grows, the refresh cost grows with it — `observation_recent_idx` and
`observation_activity_idx` (`db/007`) keep the detectors' own filtered scans
index-backed, but the materialized view's own `WITH ordered AS (...
lead() ...)` window computation is inherently a full pass over
`sor.observation` for the engagement each refresh, bounded only by the
`p_lookback` interval detectors pass to themselves (default 90 days) — not
by the view's own refresh, which recomputes over the view's own unfiltered
window. If an engagement's observation volume grows large enough that a
6-hourly full recompute becomes the bottleneck, the fix is narrowing what
`sor.observed_transition` itself windows over (adding a lookback filter to
the view definition), not changing the scan cadence.
