# FDE Agent Platform — Architecture

Three agents, one knowledge graph, deterministic human gates between them.

This document is the map. Read it before the individual blueprints.

---

## 1. What the three pillars actually are

The source slide names three agents. Restated as engineering contracts:

| Pillar | What it does | Its relationship to the graph |
|---|---|---|
| **Engagement Agent** | On-site assistant. Interviews SMEs, maps workflows, detects bottlenecks, scores AI opportunity. | **Writer.** Everything it produces is a *proposal*, never a write. |
| **Workflow Agent** | Remodels how work gets done. Designs agent workflows, maps systems and handoffs, owns state/routing logic. Monitors drift. | **Reader + author.** Authors workflows pinned to a graph commit; monitors reality against them. |
| **Development Agent** | Builds end-to-end workflow agents. Evals, guardrails, full tool calling. | **Meta.** Consumes published workflows, emits deployable agent packages. |

They form a loop, not a pipeline:

```
   ┌──────────────────────────────────────────────────────────────┐
   │                                                              │
   ▼                                                              │
Engagement ──proposal──▶ [HUMAN GATE] ──merge──▶ Knowledge Graph  │
   Agent                                              │           │
                                                      │           │
                                    ┌─────────────────┘           │
                                    ▼                             │
                            Workflow Agent ──▶ published workflow │
                                    │                    │        │
                                    │                    ▼        │
                                    │           Development Agent │
                                    │                    │        │
                                    │                    ▼        │
                                    │            deployed agents   │
                                    │                    │        │
                                    ▼                    ▼        │
                            drift monitor ◀────── system of record │
                                    │                             │
                                    └──── drift signal ───────────┘
```

The loop closes because the drift monitor's output is a *new proposal* back into the
Engagement Agent's queue. That is what makes the graph a living model of the business
rather than a document that was true once.

---

## 2. The one invariant everything else hangs off

> **Agents propose. Humans dispose. Only `hitl.merge_proposal` writes the graph.**

This is enforced at four independent layers, so removing any one of them does not
open the door:

1. **Grant.** `fde_agent` has no `INSERT`/`UPDATE`/`DELETE` on `kg.node`, `kg.edge`,
   or `kg.commit`. Explicitly `REVOKE`d in `db/010`.
2. **Function.** `hitl.merge_proposal` is `SECURITY DEFINER` and `REVOKE`d from
   `PUBLIC`. Only `fde_gate_service` may execute it.
3. **Precondition.** The function re-checks `hitl.gates_satisfied()` *inside its own
   transaction*, not just at the UI layer — an authority revoked between approval and
   merge still blocks the merge.
4. **Fail-closed submit.** `hitl.submit_proposal` raises if *no* gate policy matched,
   rather than treating "no policy" as "no review needed."

Smoke tests 2, 3, and 4 in `db/tests/smoke_test.sql` exist specifically to prove
layers 2–4 hold. They pass on a clean rebuild.

### Why "deterministic" is the load-bearing word

The gate set required to merge a proposal is computed by
`hitl.compute_required_gates(proposal_id)` — a pure SQL function over the
`hitl.gate_policy` table. Given the same proposal contents and the same active
policy, it always returns the same gates. No model is in that decision.

This matters for three separate reasons, and it is worth being explicit because
it is the design decision most likely to be argued with:

- **Auditability.** "Why did this need compliance sign-off?" has an answer that is
  a row in a table, not a model output you cannot reproduce.
- **Non-negotiability.** The agent that authored the proposal cannot talk its way
  into a lighter review. It does not participate in the decision.
- **Training validity.** Because the gate is deterministic and the reviewer is a
  human, a merged proposal is a genuine label. If an LLM decided which proposals
  needed review, the training signal would be partly the model grading itself.

The gate set is also **materialised at submit time** into `hitl.proposal_gate`
rather than computed on read, so editing a policy cannot move the bar underneath a
review already in flight.

---

## 3. Component map

```
┌─────────────────────────────────────────────────────────────────────────┐
│ AWS ACCOUNT / VPC                                                       │
│                                                                         │
│  ┌────────────────── Bedrock AgentCore ─────────────────────────────┐   │
│  │                                                                  │   │
│  │  Runtime: fde-engagement    Runtime: fde-workflow                │   │
│  │  Runtime: fde-development                                        │   │
│  │      (ARM64 containers OR CodeZip, 1 microVM per session)        │   │
│  │                          │                                       │   │
│  │  Gateway (MCP, semantic tool search, CUSTOM_JWT inbound)         │   │
│  │      ├── target: kg-mcp-server   (MCP server target)             │   │
│  │      ├── target: sor-adapters    (Lambda target)                 │   │
│  │      └── target: gate-service    (Lambda target)                 │   │
│  │                          │                                       │   │
│  │  Memory (semantic + summary + userPreference strategies)         │   │
│  │  Identity (workload identity, OAuth token vault for SoRs)        │   │
│  │  Observability -> CloudWatch GenAI (session/trace/span)          │   │
│  │  Evaluations (17 built-in evaluators + custom Lambda graders)    │   │
│  └──────────────────────────┬───────────────────────────────────────┘   │
│                             │                                           │
│  ┌──────────────────────────▼───────────────────────────────────────┐   │
│  │ KG MCP Server  (ECS/Fargate or in-runtime, streamable HTTP :8080)│   │
│  │   kg_search · kg_traverse · kg_dependency_closure ·              │   │
│  │   kg_impact_radius · kg_process_flow · kg_propose ·              │   │
│  │   kg_submit_proposal · drift_scan · wf_draft · ...               │   │
│  └──────────────────────────┬───────────────────────────────────────┘   │
│                             │                                           │
│  ┌──────────────────────────▼───────────────────────────────────────┐   │
│  │ Aurora PostgreSQL 16 (pgvector >= 0.8.0)                         │   │
│  │   kg.*   graph, bitemporal, HNSW                                 │   │
│  │   hitl.* proposals, gate policy, decisions      <- the spine     │   │
│  │   wf.*   workflows, steps, bindings, runs                        │   │
│  │   sor.*  adapters, observations, drift signals                   │   │
│  │   trn.*  traces, failure labels, duels                           │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  Gate Service (Lambda + API GW)   Embedder Worker (ECS)                  │
│  Drift Monitor (EventBridge -> Lambda -> invoke fde-workflow)           │
│  Prod-Ops Console (the human surface: review queue, run workflows)      │
│                                                                         │
│  SageMaker: SFT / GRPO   |   Bedrock RFT (managed GRPO alternative)      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Why relational edges + pgvector, and not a graph database

The decision, stated plainly, with the cost of each alternative.

**Apache AGE is not available on RDS or Aurora.** It is a C extension outside AWS's
approved-extension allowlist. Choosing openCypher means running self-managed Postgres
on EC2/EKS and owning backup, failover, patching, and HA yourself. For a graph that
will hold 10³–10⁵ nodes per engagement, that operational cost buys query ergonomics
and nothing else.

**Neptune** would mean a second datastore. The HITL tables, the workflow tables, the
drift observations, and the training traces all need to be transactionally consistent
with the graph — `hitl.merge_proposal` writes graph rows, evidence rows, the embed
queue, and a drift signal in one transaction. Splitting the graph out means either
two-phase commit or accepting that a merge can half-succeed. Neither is worth it.

**Recursive CTEs are adequate.** `kg.traverse` uses the PostgreSQL 14+ `CYCLE` clause
for cycle detection and enforces three hard bounds (hops, node count, edge-type
allowlist). The composite partial indexes `edge_fwd_idx (engagement_id, src_key,
edge_type, dst_key) WHERE valid_to IS NULL` and its reverse make each hop an index
scan.

The honest cost of this choice: multi-hop queries are more verbose to write in SQL
than in Cypher, and there is no query planner that understands graph shape. That is
mitigated by putting every traversal behind a named SQL function (`db/008_retrieval.sql`)
so no one hand-writes traversal at a call site, and the MCP server is a thin typed
wrapper over exactly those functions.

**Where this choice would be wrong:** if the graph grows past ~10⁷ edges per
engagement, or if you need genuinely unbounded variable-length path queries with
complex path predicates. Neither is on the horizon for process-mapping work.

---

## 5. Retrieval design

Three granularities are embedded separately — nodes, edges, and evidence chunks —
because a query matches at different levels depending on what it asks:

- *"who approves a discount over 20%?"* → matches **role** and **control** nodes
- *"what happens after legal review?"* → matches a **`precedes` edge**
- *"what did the RevOps lead say about SLAs?"* → matches an **evidence chunk**

Collapsing these into one index is the most common reason GraphRAG retrieval
underperforms — the edge "Sales Rep hands off Quote Approval to Deal Desk when
discount exceeds 20%" is a *sentence* worth embedding, and it is invisible if you
only embed node summaries.

`kg.hybrid_search` runs four ranked lists and fuses them with **Reciprocal Rank
Fusion at k=60**:

| List | Source |
|---|---|
| `node_ann` | HNSW over node embeddings |
| `edge_ann` | HNSW over verbalised edges (both endpoints promoted) |
| `chunk_ann` | HNSW over evidence text (anchors promoted) |
| `graph_expand` | Bounded traversal from the top-10 node seeds |

RRF is rank-based rather than score-based on purpose: a cosine distance from an ANN
index and a hop-distance from a traversal are not on a comparable scale, and
normalising them invents a calibration you do not have. Ranks are comparable by
construction. k=60 is the standard constant.

Every result carries a `provenance` object naming which lists it came from and at
what rank. Three separate consumers depend on that being present: the Workflow
Agent must cite it, the rival grader scores against it, and the RL reward function
reads it to compute groundedness.

### The pgvector detail that is a correctness bug, not a tuning knob

On pgvector < 0.8.0, a filtered ANN query —

```sql
SELECT ... WHERE node_type = 'control' ORDER BY embedding <=> $1 LIMIT 20
```

— fetches a fixed HNSW candidate batch **before** applying the filter. On a
selective filter it returns *fewer than 20 rows, or zero*, even when matching rows
exist. It looks like "the graph doesn't know that," not like a bug.

Two defences, both used:

1. `hnsw.iterative_scan = 'relaxed_order'`, set on every connection by
   `kg.tune_session()` in the pool initialiser. Bounded by `hnsw.max_scan_tuples`
   (20,000) so a pathological filter cannot run away.
2. **Partial HNSW indexes per node type** (`db/003`). These are a *true* pre-filter —
   the index only contains rows of that type, so there is no post-filter shortfall
   at all. Smaller and faster to build, too.

`kg.tune_session` refuses `ef_search` outside 40–200. Above roughly 200 the planner
starts abandoning the HNSW index for a sequential scan on filtered queries, which is
a latency cliff rather than a gradual degradation.

### The `halfvec` cast that must not be forgotten

Storage is `vector(1024)`; the HNSW index is an **expression index** on
`(embedding::halfvec(1024))` to halve index memory. That means every probe must be
cast identically:

```sql
ORDER BY ne.embedding::halfvec(1024) <=> $1::halfvec(1024)
```

Forget the cast and the planner silently ignores the index and sequential-scans.
This is why all retrieval lives in `db/008_retrieval.sql` functions and nothing
hand-writes an ANN query at a call site.

---

## 6. Bitemporality, and why workflows pin to a commit

`kg.node` and `kg.edge` are append-only with both valid time (`valid_from`/`valid_to`
— when the fact was true of the business) and transaction time (`tx_from`/`tx_to` —
when we believed it). Nothing is ever updated in place.

Every published workflow stores `pinned_commit_id` and `pinned_digest`. That gives
three properties:

- **Reproducibility.** Six months later you can reconstruct exactly the graph the
  workflow was authored against: `kg.node_as_of(engagement, commit.sealed_at)`.
- **Detectable staleness.** `hitl.merge_proposal` raises a `stale_pin` drift signal
  for every published workflow pinned behind the new head, transactionally, at merge
  time. Nobody has to remember to check.
- **Tamper evidence.** The digest is computed over the merged items at seal time.

HNSW indexes are partial on `WHERE is_current`, so history accumulates without
growing the index. A trigger keeps `is_current` in sync when a version is closed out —
without it, retired facts keep surfacing in retrieval, which is a subtle and
expensive failure.

---

## 7. Data flows

### 7.1 Ingest → graph (the deterministic HITL path)

```
FDE captures a source (interview recording, SOP, system export)
   └─▶ kg.source row + kg.chunk rows, embedded
        └─▶ Engagement Agent reads chunks, proposes graph facts
             └─▶ hitl.proposal + hitl.proposal_item (evidence REQUIRED)
                  └─▶ hitl.submit_proposal()
                       ├─ validates: every non-retire item cites a source
                       ├─ computes required gates (pure SQL over policy)
                       ├─ MATERIALISES the gate set (frozen)
                       └─ raises if no policy matched (fail closed)
                            └─▶ reviewers decide in the prod-ops console
                                 └─▶ hitl.merge_proposal()  [gate service only]
                                      ├─ re-checks quorum in-transaction
                                      ├─ closes superseded versions
                                      ├─ inserts new versions under a sealed commit
                                      ├─ writes kg.evidence
                                      ├─ enqueues embeddings
                                      └─ raises stale_pin drift for pinned workflows
```

### 7.2 Graph → workflow

```
Workflow Agent: kg_head_commit -> pin
   └─▶ kg_process_flow + kg_traverse to derive steps and routing
        └─▶ wf_draft: workflow + steps + step_bindings
             └─▶ wf.assert_faithful()  — refuses to pass if:
                  ├─ any non-notify step has zero bindings
                  └─ any binding names a key not live at the pinned commit
                       └─▶ human publishes (agents cannot flip status)
```

Step bindings are the mechanism that makes "faithful" checkable rather than
aspirational. A step that cannot name the graph element it implements does not get
published.

### 7.3 Reality → drift

```
SoR adapters (Jira / Salesforce / ServiceNow / internal DB / event stream)
   └─▶ sor.observation  (normalised, actor hashed, mapped to activity keys)
        └─▶ sor.observed_transition  (materialised view, refreshed on scan)
             └─▶ four SQL detectors, run on a schedule:
                  · detect_sequence_drift    (missing_in_sor / missing_in_graph)
                  · detect_control_bypass    (compliance — escalates to critical)
                  · detect_actor_drift       (wrong role performing)
                  · detect_latency_drift     (SLA on the edge vs observed p50/p95)
                       └─▶ sor.drift_signal (deduped, severity, sample size)
                            └─▶ Workflow Agent triages, drafts a proposal
                                 └─▶ back into the HITL gate
```

**Detection is SQL, not an LLM.** The agent does not decide *whether* drift exists —
deterministic SQL does, with explicit minimum sample sizes so an n=3 coincidence
never reaches a human. The agent decides what to *do* about it: explain it,
prioritise it, and draft the graph change. That split keeps findings reproducible
and cheap, and keeps the agent inside its actual competence.

---

## 8. Where the humans are

Five distinct human touchpoints. Naming them explicitly, because "human in the loop"
usually means one undifferentiated approve button.

| # | Gate | Who | Blocking? | Implemented by |
|---|---|---|---|---|
| 1 | **Ontology** — is this the right type? does it duplicate? | Any authorised reviewer | Yes, on every proposal | `hitl.gate_policy` catch-all |
| 2 | **Factual** — is this how work is actually done? | SME (2 for process/capability) | Yes, when structural or weakly evidenced | policies 2, 5, 6 |
| 3 | **Control** — does this touch compliance? | Risk / compliance | Yes, no self-approval, 168h SLA | policies 3, 4, 7 |
| 4 | **Automation** — may an agent do this unattended? | Process owner | Yes, on any `automatable_by` edge | policy 5 |
| 5 | **Publish** — is this workflow fit to run? | Workflow owner | Yes; agents have `INSERT` but never `UPDATE` on `wf.workflow` | grant boundary in `db/011` |

Plus the in-run human steps: `wf.step.kind = 'human'` pauses a run and waits for an
operator, using AgentCore's `add_async_task`/`complete_async_task` so the runtime
reports `HealthyBusy` and the session is not reaped mid-wait. See
`docs/07-hitl-gates.md` for the 15-minute ping constraint that governs this.

---

## 9. Training, in one paragraph

The HITL gates are already producing high-quality human labels as a side effect of
doing the work. A merged proposal is a positive label; a rejected one is negative;
an **edited** one is the most valuable of all, because it is a paired (wrong, right)
example on the same input — a free preference dataset. `trn.trace_session` pins the
`base_commit_id` so a trace is reconstructible, and `trn.trace_step.trainable` is
`false` on every tool and user turn so the loss mask is not re-derived (and
mis-derived) downstream. SFT teaches tool-call syntax and normalised traversal
shape; GRPO targets the specific traversal failures enumerated in
`trn.traversal_failure`; the rival grader runs retriever variants head-to-head with
order-mirrored pairwise judging and Bradley-Terry aggregation. Full detail and
honest volume gates in `docs/06-training.md` — including the recommendation that
most teams should stop after SFT.

---

## 10. Reading order

1. This document
2. `docs/01-knowledge-graph.md` — ontology, schema, retrieval, operations
3. `docs/07-hitl-gates.md` — the gate model in detail
4. `docs/02-agent-engagement.md`, `03-agent-workflow.md`, `04-agent-development.md`
5. `docs/05-mcp-surface.md` — the tool contracts
6. `docs/06-training.md` — SFT → RL → graders
7. `docs/08-drift-monitor.md` — SoR adapters and detectors
8. `docs/09-deployment.md` — AgentCore, IAM, CI/CD, cost
9. `docs/10-prodops-runbook.md` — what the product operations group actually does
10. `docs/99-sources.md` — every external claim traced to a URL
