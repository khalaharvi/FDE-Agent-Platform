# Blueprint 2 — Workflow Agent

> *Remodels how work gets done with AI · Agent workflow design · System + handoff mapping · State/routing logic*
> *Plus: authors faithful workflows from the current graph, and autonomously monitors drift against the system of record.*

**Runtime name:** `fde_workflow`
**Code:** `agents/workflow/`
**Role in the graph:** reader and author. Pins a commit, authors against it, watches reality diverge from it.

---

## 1. Contract

### Identity

Takes a knowledge graph that describes how work is done today and produces two
things: **a runnable workflow** that a product operations person can execute, and
**a standing watch** on whether that workflow still matches reality.

The distinguishing constraint is faithfulness. It is not designing an ideal process
— it is expressing the graph as an executable sequence, and every step it writes
must name the graph element it implements. When it wants to improve the process, it
proposes a graph change and lets a human decide; it does not quietly encode the
improvement in a workflow step and hope nobody notices the workflow and the graph
have diverged.

### Tasks

| Task | Input | Output | Invoked by |
|---|---|---|---|
| `author_workflow` | `process_key`, `autonomy_target` | draft workflow + steps + bindings, `assert_faithful` passed | human |
| `monitor_drift` | `engagement_id` | drift scan + triage judgements + drafted proposals | **EventBridge schedule** |
| `triage_drift` | `signal_id` | severity judgement, recommendation, optional proposal | human or self |
| `reauthor_stale` | `workflow_id` | new draft version pinned to head + a diff | human |

`monitor_drift` is the autonomous path. Everything else is human-initiated.

---

## 2. Authoring: how a graph becomes a workflow

The mapping is mechanical by design. The agent is not inventing structure; it is
translating one.

| Graph element | Becomes |
|---|---|
| `activity` node | a `wf.step` |
| `precedes` edge | step ordering |
| `hands_off_to` edge | a step boundary + a `notify` step to the receiving role |
| `gated_by` → `control` | a `human` step, `requires_human = true`, always |
| `gated_by` → `decision` | a `decision` step, branches from `edge.attributes.conditions` |
| `performs` edge | the step's assigned role → who the run pauses for |
| `recorded_in` edge | an `sor_write` step |
| `depends_on` → `system` | a precondition check + the adapter to use |
| `automatable_by` edge | permission to make the step `kind='agent'` instead of `'human'` |
| `measured_by` → `metric` | run-level instrumentation |

**The `automatable_by` rule is absolute.** A step may only be `kind='agent'` or
`kind='tool'` if the graph carries an `automatable_by` edge for that activity — and
that edge required a process-owner sign-off at gate policy 5 to exist. The agent
cannot grant itself autonomy. If the edge is absent the step is `kind='human'`,
full stop, and the agent says so in its output: *"Step 4 is human because no
`automatable_by` edge exists for `act.credit_check`. To automate it, propose one and
get process-owner approval."*

That sentence is the whole governance model in one line, and it is worth making the
agent say it out loud every time rather than silently defaulting.

### Autonomy levels

`wf.workflow.autonomy_level` is derived, not chosen:

| Level | Condition |
|---|---|
| `manual` | no step is automatable |
| `assisted` | agent drafts each step's output, human approves every one |
| `supervised` | agent executes; humans gate only steps with a `gated_by` control |
| `autonomous` | every step has `automatable_by` **and** no `gated_by` control anywhere in the flow |

`autonomous` is rare and should stay rare. A workflow that touches a compliance
control never reaches it, by construction.

### The publication gate

`wf.assert_faithful()` refuses to pass if:

1. Any non-`notify` step has zero bindings, or
2. Any binding names a key not live at the pinned commit.

The agent calls this itself via `wf_draft` and must fix what it reports. Publication
is still a human action — `fde_agent` has `INSERT` but never `UPDATE` on
`wf.workflow`, so it structurally cannot flip `status` to `published`. That is a
grant boundary, not a policy someone can forget to enforce.

---

## 3. Drift monitoring

### Two axes, genuinely different problems

**Reality drift** — the graph says one thing, the SoR shows another. The graph is
stale, or the business changed. Resolution: propose a graph change.

**Pin drift** — a published workflow was authored against commit N, the graph is now
at N+k, and something it binds to has changed underneath it. Resolution: re-author.
Raised transactionally by `hitl.merge_proposal` at merge time, so it cannot be
missed.

### Detection is SQL

Four detectors in `db/007_drift.sql`, run by `sor.run_all_detectors()`:

| Detector | Compares | Fires when | Default severity |
|---|---|---|---|
| `detect_sequence_drift` | `precedes`/`hands_off_to` vs `sor.observed_transition` | modelled transition observed < 5% of the time (n≥20), **or** an unmodelled transition observed ≥20 times | high / medium |
| `detect_control_bypass` | `gated_by` vs per-case observation | any case reached the activity without the control (n≥10) | **critical** above 10% bypass |
| `detect_actor_drift` | `performs` vs `actor_role_key` | >15% performed by an unmodelled role (n≥20) | medium |
| `detect_latency_drift` | `edge.attributes.sla_seconds` vs observed p50/p95 | p95 > SLA | high if p50 > SLA |

The agent does not decide whether drift exists. That is deliberate:

- **Reproducible.** Two people running the scan get the same answer.
- **Cheap.** A scan is a handful of SQL statements, not a model invocation per edge.
- **Honest about n.** Minimum sample sizes are in the SQL. An n=3 coincidence never
  reaches a human, which is what keeps the queue credible.
- **In-competence.** The agent's job — explain it, prioritise it, draft the fix — is
  what models are actually good at.

### Triage rules

Stated explicitly in the prompt so triage is consistent run to run:

```
IF drift_kind = 'control_bypass' AND severity >= 'high':
    ALWAYS escalate. Never auto-dismiss. Never batch with anything else.
    Draft a proposal ONLY if the graph is wrong; if the graph is right and
    the business is bypassing a real control, that is a compliance finding,
    not a modelling error. Say which one it is, explicitly.

IF sample_size < detector minimum:            → dismiss as insufficient evidence
IF drift_kind = 'missing_in_graph' AND observed_count >= 20:
                                              → draft add_edge proposal
IF drift_kind = 'missing_in_sor' AND observed_rate = 0 AND n >= 50:
                                              → draft retire_edge proposal
IF drift_kind = 'stale_pin' AND commits_behind <= 2
   AND no bound key changed:                  → dismiss as benign
IF drift_kind = 'stale_pin' AND a bound key changed:
                                              → recommend reauthor_stale
IF drift_kind = 'latency_drift':              → do NOT propose a graph change.
    An SLA breach is an operational finding. Report it; changing the modelled
    SLA to match observed reality is laundering a problem into a spec.
```

That last rule is the one people get wrong. An agent that "fixes" drift by updating
the model to match whatever is happening will, over a year, erase every standard the
organisation had.

### The control_bypass distinction

`detect_control_bypass` firing means one of two very different things:

1. **The graph is wrong** — the control is not actually mandatory, or applies only
   under conditions the graph does not model. Fix: propose an `update_edge` adding
   the condition.
2. **The graph is right and the control is being bypassed** — a genuine compliance
   finding.

The agent must state which, with evidence, and must not default to (1) because it is
the one it can fix with a proposal. The prompt is explicit: *"If you cannot
distinguish, say you cannot distinguish, and escalate to compliance. Proposing a
graph change to make a bypass signal disappear is the worst thing you can do in this
role."*

### Autonomous operation

```
EventBridge (rate: 6 hours)
  └─▶ Lambda: for each active engagement
       └─▶ invoke_agent_runtime(fde_workflow, {"task":"monitor_drift", ...})
            └─▶ drift_scan  (SQL detectors)
                 └─▶ for each signal above threshold:
                      ├─ triage judgement written to sor.drift_signal
                      ├─ where warranted: kg_propose (NEVER submit unattended)
                      └─ critical: SNS to the compliance channel immediately
```

Autonomy boundaries, and they are hard:

- May write triage state and notes. May draft proposals.
- **May not** call `kg_submit_proposal` in an unattended run. A drafted proposal sits
  in `draft` until a human submits it. Rationale: submission starts SLA clocks and
  pages reviewers, and an agent that can page people at 3am without a human in the
  loop will eventually do so wrongly and burn the queue's credibility.
- **May not** set `drift_signal.state = 'resolved'`. Enforced in `drift_triage`.
- **May not** touch any published workflow.

---

## 4. Tool allowlist

| Tool | Purpose |
|---|---|
| `kg_head_commit` | pin, every session |
| `kg_search`, `kg_traverse`, `kg_get_node` | read the graph |
| `kg_process_flow` | the primary authoring input |
| `kg_dependency_closure` | preconditions for a step |
| `kg_impact_radius` | blast radius before recommending a change |
| `wf_list`, `wf_get` | existing workflows, `commits_behind` |
| `wf_draft` | author; runs `assert_faithful` |
| `drift_scan`, `drift_list`, `drift_triage` | the monitoring loop |
| `kg_propose` | draft a graph fix |
| `kg_submit_proposal` | **human-initiated tasks only** |

---

## 5. Memory

```python
memoryStrategies=[
  {"semanticMemoryStrategy": {"name": "workflow-design-decisions",
     "namespaceTemplates": ["/strategy/{strategyId}/actor/{actorId}"]}},
  {"summaryMemoryStrategy":  {"name": "drift-history",
     "namespaceTemplates": ["/strategy/{strategyId}/actor/{actorId}"]}},
]
```

`drift-history` is the one that earns its place. A signal that has recurred across
six scans and been dismissed twice by different reviewers is a different object from
a fresh one, and the agent should say so rather than re-litigating it from scratch
every six hours. `sor.drift_signal.occurrences` carries the count; Memory carries
the *reasoning* from prior triages.

---

## 6. Guardrails

> **Naming note.** The table below is the *design intent* for the guard set. The
> shipped `agents/common/guardrails.py` implements the load-bearing subset under
> its own names -- `no_person_name_in_role_label`, `no_unbound_step` /
> `no_unbound_steps`, and `citation_required`, aggregated by `check_proposal_item`,
> `check_workflow`, and `check_turn`. The remaining rows are specified here and
> not yet implemented; treat them as the backlog for that module, not as shipped
> behaviour.


| Guard | Trigger | Action |
|---|---|---|
| `binding_required` | a drafted step with no binding | block the draft |
| `autonomy_requires_edge` | `kind='agent'` without `automatable_by` | force `kind='human'`, explain |
| `control_step_is_human` | a step bound to a `control` not marked `requires_human` | force it |
| `no_publish` | any attempt to set `status='published'` | block (also a grant boundary) |
| `no_unattended_submit` | `kg_submit_proposal` in a scheduled run | block |
| `no_sla_laundering` | a proposal that edits `sla_seconds` in response to `latency_drift` | block, explain |
| `pin_declared` | output references graph facts without naming the pinned commit | fail the turn |

---

## 7. Evals

**SESSION**
- `Builtin.GoalSuccessRate`
- `Builtin.TrajectoryInOrderMatch` — `kg_head_commit` → `kg_process_flow` → `wf_draft`
- `Builtin.TrajectoryAnyOrderMatch` — for `monitor_drift`, where ordering is not fixed

**TRACE**
- `Builtin.Faithfulness` — the central metric for this agent
- `Builtin.ContextRelevance`, `Builtin.Correctness`, `Builtin.Coherence`
- `Builtin.Conciseness` — triage output goes to busy operators

**TOOL_CALL**
- `Builtin.ToolSelectionAccuracy`, `Builtin.ToolParameterAccuracy`

**Custom Lambda evaluators**

| Evaluator | Level | Checks |
|---|---|---|
| `BindingCoverage` | SESSION | every non-notify step bound to a key live at the pin |
| `AutonomyDiscipline` | SESSION | no agent-kind step lacking `automatable_by` |
| `TriagePrecision` | TRACE | agent severity vs the human's eventual severity, over history |
| `ProposalAcceptanceRate` | SESSION | fraction of drafted proposals a human eventually submits |
| `NoSlaLaundering` | TRACE | no proposal edits an SLA in response to a latency signal |

`TriagePrecision` and `ProposalAcceptanceRate` are the two that tell you whether
this agent is actually earning its schedule. If acceptance is under ~40%, the agent
is generating work rather than removing it, and the triage thresholds need raising
before anything else gets tuned.

---

## 8. Deployment

Same runtime shape as the Engagement Agent. Two differences:

**Scheduled invocation.** EventBridge → Lambda → `invoke_agent_runtime` with
`{"task":"monitor_drift"}`. The Lambda supplies a deterministic
`runtimeSessionId` per (engagement, scan window) so repeated scans are traceable
and idempotent-ish.

**Do not use the Step Functions direct integration for HITL.** The
`arn:aws:states:::bedrockagentcore:invokeHarness` integration is
**Request-Response only** — `.sync` and `waitForTaskToken` are explicitly *not*
supported, and it is capped at 15 minutes regardless of `TimeoutSeconds`. It also
returns only the final assistant message. It is unsuitable as the basis for a
pause-for-approval flow. Use the Runtime's `add_async_task`/`complete_async_task`
with your own approval store instead (see `docs/07-hitl-gates.md`).

---

## 9. Failure modes worth designing against

**SLA laundering.** Covered above. The `no_sla_laundering` guardrail and the
explicit triage rule exist because this is the most natural wrong thing for a
helpful model to do.

**Drift queue fatigue.** A noisy queue gets ignored, and an ignored queue is worse
than no queue because it creates the appearance of monitoring. Mitigated by
deduplication on the natural key (`drift_signal_dedup_uq`), minimum sample sizes in
SQL, and `occurrences` so recurrence is visible rather than repeated. Watch queue
depth and median time-to-triage; if depth grows monotonically, raise thresholds.

**Workflow/graph divergence.** The thing faithfulness exists to prevent. Enforced by
`assert_faithful` at draft time and `stale_pin` signals at merge time. The residual
risk is a workflow whose *instruction text* drifts from its binding while the
binding stays valid — bindings prove a step relates to a graph element, not that the
prose is accurate. `Builtin.Faithfulness` at TRACE level is the backstop.

**Autonomy creep.** An agent that gradually classifies more steps as automatable.
Structurally blocked: `automatable_by` edges only exist after a process-owner gate,
and this agent cannot merge. Worth monitoring the count of `automatable_by` edges
per engagement over time regardless — a sharp rise means someone is rubber-stamping
the automation gate.
