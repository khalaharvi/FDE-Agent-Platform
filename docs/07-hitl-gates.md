# The Human-in-the-Loop Gate Model

This is the document to read if you only read one. Everything else in the platform
is downstream of the guarantees described here.

---

## 1. What "deterministic" means, precisely

The set of human approvals required to merge a proposal is computed by
`hitl.compute_required_gates(proposal_id)` — a pure SQL function over the
`hitl.gate_policy` table.

```sql
SELECT * FROM hitl.compute_required_gates(1042);
-- policy_id | gate_kind  | quorum | allow_self | sla_hours | triggering_items
-- ----------+------------+--------+------------+-----------+-----------------
--         1 | ontology   |      1 | f          |        72 | {1,2,3,4,5,6,...}
--         2 | factual    |      2 | f          |       120 | {1}
--         3 | control    |      1 | f          |       168 | {4}
--         4 | control    |      1 | f          |       168 | {10}
```

Given the same proposal contents and the same active policy rows, it returns the
same gates. Every time. There is no model in that decision, no heuristic in
application code, and no path by which the agent that authored the proposal can
influence it.

Three things follow, and each one is a requirement somewhere else in the system:

**Auditability.** "Why did this need compliance sign-off?" has an answer that is a
row in a table you can `SELECT`, not a model output you cannot reproduce. When an
auditor asks how you decide what gets reviewed, you show them
`hitl.gate_policy` — eight rows.

**Non-negotiability.** The proposing agent does not participate. It cannot argue for
a lighter review, cannot self-approve (`allow_self = false` on every shipped
policy), and cannot route around a gate by rephrasing.

**Training validity.** This is the one people miss. Because the gate is
deterministic and the reviewer is human, a merged proposal is a *genuine* label. If
an LLM decided which proposals needed review, the training signal in
`trn.trace_session.outcome` would be partly the model grading itself, and the whole
SFT/RL pipeline in `docs/06-training.md` would be training on its own output.

### The gate set is frozen at submit

`hitl.submit_proposal` **materialises** the computed gates into `hitl.proposal_gate`
rather than leaving them to be computed on read. Consequence: editing a policy
cannot move the bar underneath a review already in flight. A reviewer who started
looking at a proposal under a 1-reviewer factual gate does not discover mid-review
that it now needs 2.

The trade is that a policy fix does not retroactively apply to in-flight proposals.
That is the correct trade — the alternative is a review process whose requirements
change while people are working, which destroys trust in it faster than any bug.

---

## 2. The four gate kinds

| Kind | Question it answers | Who clears it | Default quorum | Default SLA |
|---|---|---|---|---|
| `ontology` | Is this the right node/edge type? Does it duplicate something? | any authorised reviewer | 1 | 72h |
| `factual` | Is this how work is actually done? | SME | 1, or 2 for `process`/`capability` | 72–120h |
| `control` | Does this touch a compliance control? | risk / compliance | 1 | 168h |
| `automation` | May an agent do this unattended? | process owner | 1 | 120h |

These are genuinely different questions asked of genuinely different people.
Collapsing them into one "approve" button is the most common way HITL gets
implemented and the reason it usually stops working: an SME asked to approve a
compliance question either rubber-stamps it or blocks on someone else, and either
way the signal is gone.

### The shipped policy (db/010)

| # | Name | Fires on | Quorum | SLA |
|---|---|---|---|---|
| 1 | catch-all ontology review | **everything** | 1 | 72h |
| 2 | new process/capability requires SME quorum | `add_node` of `process`\|`capability` | **2** | 120h |
| 3 | control changes require compliance | any `control` node | 1 | 168h |
| 4 | gated_by edges require compliance | any `gated_by` edge | 1 | 168h |
| 5 | automation claims require process owner | any `automatable_by` edge | 1 | 120h |
| 6 | weak evidence requires SME confirmation | evidence strength < 0.65 | 1 | 72h |
| 7 | retirement requires confirmation | `retire_node`\|`retire_edge` | 1 | 72h |
| 8 | regulated data touchpoints | `$.attributes.data_classification == "regulated"` | 1 | 168h |

Rules are **additive**. A proposal adding a new process that touches a control and
carries weak evidence requires all of ontology + factual(2) + control + factual —
four gates, five distinct approvals.

**Do not remove rule 1.** It is what makes the fail-closed check in
`submit_proposal` reachable rather than a permanent exception. Without it, a
proposal matching no policy would hit the "no gate policy matched, refusing to
submit ungated" exception every time, and the pressure to bypass that check would
be immediate.

---

## 3. Fail-closed, in four places

```sql
-- 1. Evidence is mandatory.
IF n_unsourced > 0 THEN
  RAISE EXCEPTION 'proposal % has % item(s) with no evidence source; '
                  'every asserted fact must cite a kg.source', ...;
END IF;

-- 2. An ungated proposal cannot be submitted.
IF NOT EXISTS (SELECT 1 FROM hitl.proposal_gate WHERE proposal_id = p_proposal_id) THEN
  RAISE EXCEPTION 'no gate policy matched proposal %; refusing to submit ungated '
                  '(add a catch-all policy rather than bypassing this check)', ...;
END IF;

-- 3. Quorum counts only DISTINCT, currently-AUTHORISED, NON-SELF approvals,
--    and any live reject/request_changes blocks regardless of approvals.
--    See hitl.gates_satisfied().

-- 4. Merge re-checks gates INSIDE its own transaction.
IF NOT hitl.gates_satisfied(p_proposal_id) THEN
  RAISE EXCEPTION 'gates not satisfied for proposal % at merge time', ...;
END IF;
```

Point 4 is not paranoia. Between a UI marking a proposal approved and the merge
landing, an authority can be revoked, a reviewer can be deactivated, or a late
reject can arrive. Checking only at the UI layer means a race that produces an
unreviewed graph write, and it will happen at the worst possible moment.

Smoke tests 2, 3, and 4 exist to prove these hold:

```
TEST 2: merge is REFUSED before gates clear
        -- status forced to 'approved', merge attempted, must raise,
        -- and kg.node must still be empty afterwards
TEST 3: partial quorum still blocks
TEST 4: unauthorised approval does not count
        -- a reviewer with no `control` authority approves a control gate;
        -- gates_satisfied() must still be false
```

All three pass on a clean rebuild (`db/rebuild.sh`).

---

## 4. The authority model

```
hitl.reviewer                  -- the only place a real human identity lives
  └─ hitl.reviewer_authority   -- (reviewer, engagement, gate_kind) with an
                                  optional node_type scope, and a revoked_at
```

Authority is per **engagement** and per **gate kind**. A compliance reviewer on the
Q2C engagement is not automatically a compliance reviewer on the next one. Grants
carry `granted_by` and are revoked by setting `revoked_at`, never deleted — the
history of who could approve what, when, is itself audit material.

`hitl.gates_satisfied` joins through `reviewer_authority` with
`revoked_at IS NULL` and `reviewer.is_active`, so a revocation takes effect
immediately, including for approvals already recorded.

### Decisions are append-only

`hitl.gate_decision` never updates. A reviewer changing their mind inserts a new row
and the old one gets `superseded_by` set. The partial unique index
`gate_decision_live_uq ON (gate_id, reviewer_id) WHERE superseded_by IS NULL`
enforces exactly one live decision per reviewer per gate.

`review_seconds` is recorded on every decision. This is not surveillance — it is the
only defence against the failure mode described below.

---

## 5. The rubber-stamp problem

The most dangerous failure in any HITL system, because it looks exactly like the
system working. Green dashboard, gates cleared, everything merged.

A four-second approval on a thirty-item proposal is not a review. It is worse than
no review, because downstream everything treats it as human-confirmed: `kg.edge`
gets `human_confirmed = true`, the workflow author trusts the edge, and the training
pipeline files the trace as a positive label.

Defences, in order of how much they help:

1. **Measure it.** `review_seconds` on every decision. Report median review time per
   reviewer, per gate kind, in the prod-ops dashboard. The distribution tells you
   more than any individual number.
2. **Exclude it from training.** The SFT export should filter traces whose approving
   decision fell below a per-gate-kind floor. A rubber stamp is not a label.
3. **Show the reviewer only what they are being asked about.**
   `hitl.proposal_gate.triggering_items` records exactly which items fired this gate.
   A compliance reviewer looking at a 30-item proposal should see the two control
   items, not all thirty. This is the highest-leverage fix and it is a UI decision.
4. **Make per-item verdicts cheap.** `gate_decision.item_verdicts` lets a reviewer
   approve 7 of 9 and drop 2. If the only affordance is approve-or-reject-everything,
   reviewers will approve everything.
5. **Quorum where it matters.** Two independent SMEs on structural claims (policy 2).

### The reviewer edit is the most valuable event in the system

When a reviewer edits an item's payload rather than accepting or rejecting it,
`proposal_item.original_payload` keeps the agent's version and `payload` holds the
corrected one. That is a paired (wrong, right) example on identical input — a free
preference pair for DPO, and the single highest-signal training data this platform
produces. `training/export_sft.py --pairs` exports exactly these.

Design the review UI to make editing as easy as approving. Every edit is worth more
than ten approvals.

---

## 6. In-run human steps, and the constraint that governs them

Separate from proposal gates: a workflow step with `kind = 'human'` pauses a run and
waits for an operator.

The implementation constraint is specific and unforgiving:

> **AgentCore reaps a session after 15 minutes of unresponsive pings.**
> Max session lifetime is 8 hours. Idle timeout is 15 minutes.

So a human step must:

1. Register an async task so the runtime reports `HealthyBusy`:
   ```python
   task_id = app.add_async_task("hitl:step:credit_approval",
                                {"run_step_id": rs_id})
   ```
   While any task is active, the automatic ping status is `HEALTHY_BUSY` with a
   fresh `time_of_last_update`. That is what keeps the session alive.

2. **Never block the event loop.** The wait must run on a separate asyncio task or
   thread. If the approval poll blocks, `/ping` stops responding, and the session is
   torn down 15 minutes later with the operator's work in flight.
   `agents/common/hitl.py` implements this correctly — read it before writing
   another one.

3. Complete the task on resolution *or* timeout:
   ```python
   app.complete_async_task(task_id)   # returns bool; False means unknown id
   ```
   Note `add_async_task` returns an **int** (a hash of a fresh UUID4), not a string.

4. Respect the 8-hour ceiling. A step that may wait overnight cannot be a live
   session wait. Persist the pending step to `wf.run_step` with
   `status = 'awaiting_human'`, let the session end, and resume in a fresh session
   when the operator responds. `wf.run.runtime_session_id` and
   `run_step.async_task_id` exist so the resume can reattach.

### What not to use

The Step Functions direct integration
(`arn:aws:states:::bedrockagentcore:invokeHarness`) supports **Request-Response
only**. `.sync` and `waitForTaskToken` are explicitly not supported, and it is
capped at 15 minutes regardless of `TimeoutSeconds`. It also returns only the final
assistant message, dropping intermediate turns and tool-use blocks.

It is not a basis for a pause-for-approval flow. If you want Step Functions
orchestration around a human approval, run a Standard workflow with
`waitForTaskToken` against **your own** approval API, and have that API call
`complete_async_task` on the agent side. The task token lives in Step Functions;
the agent just waits.

---

## 7. The merge, end to end

```sql
SELECT * FROM hitl.merge_proposal(1042, 'owner@example.com');
```

In one transaction, as `SECURITY DEFINER`:

1. Lock the proposal (`FOR UPDATE`), assert `status = 'approved'`
2. **Re-check `gates_satisfied()`**
3. Create a sealed `kg.commit` chained to the previous head
4. For each non-dropped item, in ordinal order:
   - close the current version (`valid_to := now()`)
   - insert the new version under the commit
   - set `superseded_by` where a supersession was declared
   - enqueue an embedding
   - accumulate the content digest
5. Write `kg.evidence` rows for every (item, source) pair, noisy-OR-merging
   confidence on conflict
6. Update the commit's `content_digest`
7. Mark the proposal `merged`
8. **Raise a `stale_pin` drift signal for every published workflow pinned behind the
   new head** — transactionally, so it cannot be missed

Step 8 is why drift detection does not need to poll for staleness. The moment the
graph moves, every workflow that might now be wrong is flagged.

### One design note on timestamps

`v_now := now()` — transaction timestamp, deliberately not `clock_timestamp()`.
Reads default to `now()` too, so a merge and any subsequent read in the same
transaction agree. With `clock_timestamp()`, newly-written rows have `valid_from`
slightly *after* the reading transaction's `now()`, and `kg.traverse` silently
returns zero rows. That is a read-your-own-write failure that only shows up in test
harnesses and batch jobs — this repo hit it, and the fix is documented in
`db/005_merge.sql` at the declaration.

A useful side effect: merging the same key twice inside one transaction violates
`valid_to > valid_from` and aborts, which is the correct behaviour anyway.

---

## 8. Tuning gate policy for a real engagement

Start with the eight shipped rules. Then:

**Raise quorum where a wrong answer is expensive and cheap to catch** — structural
claims (process boundaries), anything with regulatory exposure. Do not raise quorum
broadly; a 2-reviewer default on everything guarantees rubber-stamping because
nobody has time.

**Tune `min_evidence_strength` from observed data, not intuition.** After a few
hundred merges, look at the correlation between evidence strength and whether the
item was edited. If items at 0.7 get edited as often as items at 0.5, the threshold
is in the wrong place.

**Set SLAs from reviewer availability, not from wishes.** A 24-hour SLA on a
compliance gate where the reviewer is in another timezone and has a day job produces
expired proposals, and an expiry queue nobody looks at is a worse outcome than a
longer SLA.

**Scope authority narrowly and review it.** `reviewer_authority.node_types` lets you
say "this SME may clear factual gates, but only for `activity` and `artifact`
nodes." Use it. Broad authority granted once and never revisited is how a gate
model quietly becomes a formality.

**Watch these four numbers.** They are the health of the whole system:

| Metric | Healthy | Warning sign |
|---|---|---|
| Median `review_seconds` by gate kind | proportional to item count | flat and small = rubber-stamping |
| Edit rate (items edited / items reviewed) | 10–30% | near 0% = not reading; near 60% = agent miscalibrated |
| Expiry rate | < 5% | rising = SLA or staffing problem |
| Time from submit to merge, p50 and p90 | stable | p90 growing = a specific reviewer is the bottleneck |
