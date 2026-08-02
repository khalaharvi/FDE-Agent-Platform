# The Knowledge Graph — Ontology, Bitemporality, Retrieval, Operations

The graph is `kg.*`. Everything else in the platform — proposals, workflows,
drift signals, training traces — either writes into it (through exactly one
gated path) or reads it. This document is the schema plus the reasoning that
produced it. Line numbers below refer to the migrations as shipped
(`db/001_extensions_and_types.sql`, `db/002_graph_core.sql`,
`db/003_vectors_hnsw.sql`, `db/008_retrieval.sql`).

---

## 1. The ontology

Fifteen node types, fifteen edge types, both `ENUM`s (`kg.node_type`,
`kg.edge_type` in `db/001`). Closed by construction — you cannot insert a row
whose type isn't in the enum, Postgres rejects it at the type level, before
any application code runs.

### 1.1 Node types

| Type | Definition | Quote-to-Cash example |
|---|---|---|
| `org_unit` | Team, department, or function | "Deal Desk" |
| `role` | A job role — never a named person | "Deal Desk Analyst" |
| `system` | A system of record or tool | "CPQ", "Salesforce", "NetSuite" |
| `system_object` | An entity that lives inside a system | "Opportunity", "Quote", "Invoice" |
| `capability` | A business capability | "Pricing Governance" |
| `process` | An end-to-end business process | "Quote to Cash" |
| `activity` | One step performed within a process | "Create Quote", "Discount Review" |
| `artifact` | A document or data object produced/consumed | "Signed Order Form" |
| `decision` | An explicit decision point with branch conditions | "Discount tier routing" |
| `control` | A policy, compliance, or approval control | "20% Discount Threshold" |
| `metric` | A KPI or operational measure | "Quote Cycle Time (p95)" |
| `pain_point` | An observed bottleneck or friction | "Deal Desk backlog on Fridays" |
| `opportunity` | A scored AI/automation opportunity | "Auto-approve sub-10% discounts" |
| `tool_binding` | A concrete callable tool an agent could use | "CPQ discount-check API" |
| `evidence_doc` | Interview note, SOP, screen recording, ticket export | "Discount Approval SOP v4" |

### 1.2 Edge types

Each edge type has a fixed set of legal endpoint node types. The schema does
not enforce this with a `CHECK` (the legal pairs are a modelling convention,
not a hard constraint, because a handful of edge types are legitimately
polymorphic — `depends_on` and `evidenced_by` in particular). Treat the table
below as the contract the Engagement Agent's prompt states verbatim, and that
`kg_propose` callers are expected to honor.

| Type | Definition | Legal src → dst | Q2C example |
|---|---|---|---|
| `belongs_to` | Membership | `activity`→`process`; `role`→`org_unit` | Discount Review belongs_to Quote to Cash |
| `performs` | Who does the work | `role`→`activity` | Deal Desk Analyst performs Discount Review |
| `precedes` | Control flow | `activity`→`activity` | Create Quote precedes Discount Review |
| `hands_off_to` | Control flow across a role/system boundary | `activity`→`activity` | Create Quote hands_off_to Discount Review |
| `produces` | Output | `activity`→`artifact` | Discount Review produces Approval Record |
| `consumes` | Input | `activity`→`artifact` | Discount Review consumes Quote Draft |
| `recorded_in` | Where the fact lands | `activity`→`system_object` | Create Quote recorded_in Quote (CPQ) |
| `depends_on` | Hard dependency | `activity`→`system`\|`artifact`\|`activity` | Discount Review depends_on CPQ |
| `gated_by` | Approval/branch gate | `activity`→`decision`\|`control` | Create Quote gated_by 20% Discount Threshold |
| `measured_by` | Instrumentation | `process`\|`activity`→`metric` | Quote to Cash measured_by Quote Cycle Time |
| `blocks` | Friction | `pain_point`→`activity` | Deal Desk backlog blocks Discount Review |
| `addresses` | Remedy | `opportunity`→`pain_point` | Auto-approve sub-10% addresses Deal Desk backlog |
| `automatable_by` | Automation permission (highest-consequence edge in the ontology) | `activity`→`tool_binding` | Discount Review automatable_by CPQ discount-check API |
| `evidenced_by` | Provenance pointer | any node → `evidence_doc` | Discount Review evidenced_by SOP v4 |
| `supersedes` | Lineage across a rewrite | node → node (same type, prior version) | new Discount Review supersedes old Discount Review |

`depends_on` is the backbone the platform is named for: `kg.dependency_closure`
and `kg.impact_radius` (`db/008`) both walk it, and it is the edge type
`automatable_by` decisions get checked against before anyone lets an agent
touch a step unattended.

### 1.3 Why the ontology is closed, and what that buys

Adding a sixteenth node type or edge type is a schema migration — a `pg_temp`
role cannot do it, an agent role cannot do it, and it goes through the same
human review any other schema change would. Three things follow:

- **No silent ontology drift.** An LLM given a free-text `type` field will,
  over a long engagement, invent "process_step" alongside "activity" the
  first time the exact word doesn't feel right. A closed enum makes that a
  `CheckViolation`/`InvalidTextRepresentation` the model sees and self-corrects
  from (`packages/fde-mcp/src/fde_mcp/server.py`'s error boundary re-raises exactly these verbatim —
  see `docs/05-mcp-surface.md`), not a new de facto type nobody agreed to.
- **Cross-engagement comparability.** Every engagement's graph speaks the same
  fifteen-and-fifteen vocabulary, so a retriever variant, a training example,
  or a prompt written against one engagement generalizes to the next. A
  bespoke ontology per engagement would mean bespoke prompts, bespoke
  retrieval tuning, and no reusable training data.
- **A forcing function for the right conversation.** When a real engagement
  needs a concept the ontology doesn't have, the fix is a one-line migration
  reviewed by a human who has to say what it is, what it isn't, and which
  edges may touch it — the discussion an ad hoc type would have skipped
  entirely, at the cost that later reads regret.

**Where this would be wrong:** a domain with genuinely emergent, engagement-
specific taxonomies (e.g. a research tool cataloguing arbitrary scientific
entity types) has no fixed vocabulary to close over. That is not this
platform's shape — every FDE engagement is mapping the same fifteen kinds of
thing about how a business runs work.

---

## 2. The bitemporal model

`kg.node` and `kg.edge` each carry two independent time axes (`db/002`):

| Axis | Columns | Answers |
|---|---|---|
| Valid time | `valid_from`, `valid_to` | When was this true **of the business**? |
| Transaction time | `tx_from`, `tx_to` | When did **we believe** it? |

Both `NULL` on the open end means "still true" / "still believed." Nothing is
ever `UPDATE`d or `DELETE`d in place — a change closes the current row
(`valid_to := now()`, done by `kg.close_node`/`kg.close_edge` in `db/005`) and
inserts a new one under a new commit. This is the append-only rule, and it is
absolute: there is no code path anywhere in the platform, including
`hitl.merge_proposal`, that issues an `UPDATE ... SET label = ...` against a
live row.

### 2.1 Why two axes and not one

A single "when was this true" timestamp cannot answer "what did the graph
believe on 2026-05-01, using only what we knew as of 2026-05-01" versus "what
do we now believe was true on 2026-05-01." Those are different questions with
different answers whenever a correction lands late. Example:

- On 2026-04-01, an SME says the discount threshold is 15%. Merged: `valid_from
  = 2026-04-01`, `tx_from = 2026-04-01`.
- On 2026-06-01, a second SME corrects this: it was actually 20% the whole
  time, effective 2026-01-01. Merged: the 15% row's `tx_to` closes at
  2026-06-01 (we stopped believing it then); a new row is inserted with
  `valid_from = 2026-01-01` (true of the business since January) and
  `tx_from = 2026-06-01` (believed since June).

`kg.node_as_of(engagement, at_valid, at_tx)` (`db/002`) answers both
questions independently:

```sql
-- "What did the graph say was true on 2026-05-01, using what we knew on
--  2026-05-01?" -> the 15% row, because as of 2026-05-01 tx we hadn't
--  learned about the January backdating yet.
SELECT * FROM kg.node_as_of(:eng, '2026-05-01', '2026-05-01');

-- "What do we now believe was true on 2026-05-01?" -> the 20% row.
SELECT * FROM kg.node_as_of(:eng, '2026-05-01', now());
```

Collapsing these into one timestamp either loses the ability to reconstruct
"what a workflow author actually saw" (the reproducibility property Section 3
depends on) or loses the ability to say "we used to be wrong about this and
here's when we found out" — which is exactly the audit trail a compliance
reviewer asks for.

### 2.2 `node_key` vs `node_id`

`node_key` is a stable slug (`act.discount_review`) that survives across every
re-version of that node. `node_id` is a surrogate key identifying exactly one
*version* row. `(engagement_id, node_key)` has a unique partial index over
live rows (`node_current_key_uq ... WHERE valid_to IS NULL AND tx_to IS
NULL`) — exactly one current version per key.

**Edges reference `node_key`, never `node_id`.** This is the single most
consequential modelling choice in `db/002`. If edges pointed at `node_id`,
re-versioning a node (an SME correction, a relabel, a confidence update)
would require rewriting every edge that touches it — turning a one-node
change into an unbounded fan-out write, and making "close the old version,
insert the new one" no longer atomic in the cheap way it is today. Because
edges point at the stable key, re-versioning a node is a two-row operation
(`close_node` + `INSERT`) regardless of how many edges touch it, and every
edge automatically resolves to whichever version is current at read time via
`kg.node_current`.

### 2.3 The trigger that keeps retrieval honest

`kg.sync_embedding_currency()` (`db/003`) fires `AFTER UPDATE OF valid_to,
tx_to ON kg.node`/`kg.edge` and flips `kg.node_embedding.is_current` /
`kg.edge_embedding.is_current` to match. Every HNSW index in `db/003` is a
**partial** index `WHERE is_current`. Without the trigger, a retired fact's
embedding stays inside the index forever, and `kg_search` starts surfacing
things that used to be true — the platform's most damaging quiet failure
mode, because it looks exactly like a correct search result to the model
reading it.

---

## 3. The commit model

A `kg.commit` (`db/002`) is the unit of "the graph changed." Every node and
edge row carries a `commit_id`. Commits form a linked history via `parent_id`,
scoped per engagement (`commit_one_open_per_engagement` — at most one `open`
commit per engagement at a time, which serialises graph writes).

`hitl.merge_proposal` (`db/005`) is the only function that ever inserts a
`sealed` commit. At seal time it computes `content_digest`: a running SHA-256
fold over every merged item's `(op, subject_key, effective_payload)`, in
ordinal order:

```sql
v_digest := encode(digest(v_digest || it.op || it.subject_key ||
                          v_effective::text, 'sha256'), 'hex');
```

The digest is not decorative. `wf.workflow.pinned_digest` stores a copy at
authoring time, so a workflow's own record of what it was built against is
independent of whether `kg.commit.content_digest` itself were ever tampered
with — a defense-in-depth check, since the digest lives in two places that
would have to be changed consistently.

### How workflows pin

Every workflow carries `pinned_commit_id` (`db/006`). `wf.assert_faithful`
checks bindings against `n.commit_id <= v_pin` — a binding may point at
anything true **as of** the pin, not only things created exactly at that
commit. Re-authoring against a newer commit means calling `wf_draft` again
with a new `pinned_commit_id`; the old version stays exactly as it was,
because workflows themselves are versioned rows (`UNIQUE (engagement_id, slug,
version)`), not overwritten in place.

The pin is what makes "what did the graph say when we built this" a database
query instead of institutional memory: `kg.node_as_of(engagement,
commit.sealed_at)` reconstructs it precisely, six months later, regardless of
how many corrections have landed since.

---

## 4. Provenance: sources, evidence, and evidence strength

Three tables, deliberately separate (`db/002`):

- `kg.source` — one row per captured artifact (an interview, an SOP, a system
  export). Carries `source_kind`, an S3 URI, a checksum.
- `kg.evidence` — a join table: `(subject_kind, subject_key, source_id)` plus
  an `excerpt`, a `locator` (page/timestamp/line), an `extraction_method`
  (`llm_extraction` | `human_annotation` | `rule_based` | `sor_derived`), and
  its own `confidence`.
- `kg.evidence_strength(engagement, kind, key)` — an aggregate function, not a
  column.

### 4.1 Why a join table and not a `confidence` column on the node/edge

A single scalar confidence column can only ever record the *last* thing
someone believed, and it cannot represent corroboration. Two SMEs
independently confirming the same fact is categorically stronger evidence
than one SME stating it twice, and a column can't distinguish those. A join
table lets every source contribute its own row, aggregated on read — which is
exactly what `evidence_strength` does, and it means adding a third
corroborating source later is an `INSERT`, not an overwrite that destroys the
history of who said what.

### 4.2 The noisy-OR formula, worked

```sql
SELECT COALESCE(1.0 - exp(sum(ln(greatest(1.0 - confidence, 1e-6)))), 0.0)::real
  FROM kg.evidence
 WHERE engagement_id = p_engagement AND subject_kind = p_kind AND subject_key = p_key;
```

This is `1 - PRODUCT(1 - c_i)` computed in log-space (summed logs, then
exponentiated) to avoid underflow when many sources corroborate one fact.
Treat each source's confidence as the probability that source alone would be
sufficient to establish the fact; the noisy-OR is the probability that *at
least one* of them succeeds, assuming independence.

Worked example, straight from `smoke_test.sql`'s TEST 7: `proc.quote_to_cash`
has two sources at confidence 0.92 each (an interview and an SOP that agree).
`sys.cpq` has one source at 0.97.

```
Two 0.92 sources: 1 - (1-0.92)(1-0.92) = 1 - 0.08*0.08 = 1 - 0.0064 = 0.9936
One 0.97 source:  1 - (1-0.97)          = 0.97
```

Two 0.7 sources beat one 0.9 source by the same logic: `1 - 0.3*0.3 = 0.91 >
0.9`. This is the behaviour the platform wants from corroborating interviews
— independent confirmation should outrank a single confident assertion,
because a single assertion carries the risk of one person's blind spot or
misremembering, and corroboration is direct evidence against that risk.
It is *not* the behaviour you want if sources aren't actually independent
(two people who both read the same SOP and are both restating it are one
source wearing two hats) — the formula has no way to detect that, and
`extraction_method` is the only signal available to a reviewer trying to spot
it.

`evidence_strength` feeds two consumers directly: gate policy 6
(`min_evidence_strength = 0.65` — below this a factual gate fires
unconditionally, `db/010`) and `kg.hybrid_search`'s `provenance` object
(Section 6).

---

## 5. Retrieval, in depth

### 5.1 Three granularities

`kg.node_embedding`, `kg.edge_embedding`, `kg.chunk` (`db/003`) are embedded
and indexed **separately**, because different questions match at different
levels:

| Query shape | Matches at |
|---|---|
| "Who approves a discount over 20%?" | `role`/`control` **node** |
| "What happens after legal review?" | a `precedes`/`hands_off_to` **edge** |
| "What did the RevOps lead say about SLAs?" | an **evidence chunk** |

Edges are embedded as their **verbalisation**, not the raw enum value —
`embedder_worker.py`'s `_verbalise_edge` renders `"Sales Rep hands off Quote
Approval to Deal Desk"`, folding in `attributes.condition`/`branch_condition`
and `sla_seconds` when present, never `"hands_off_to"`. An embedding model has
never seen `hands_off_to` used the way a sentence uses "hands off" — collapsing
all three granularities into one node-only index is, per `db/003`'s own
comment, the single most common reason GraphRAG retrieval underperforms.

### 5.2 RRF at k=60

`kg.hybrid_search` (`db/008`) runs four ranked lists and fuses with
Reciprocal Rank Fusion:

| List | Built from | Ranked by |
|---|---|---|
| `node_ann` | `kg.ann_nodes` — direct HNSW over node embeddings | cosine distance |
| `edge_ann` | `kg.ann_edges` — HNSW over edge verbalisations, both endpoints promoted | cosine distance |
| `chunk_ann` | `kg.ann_chunks` — HNSW over evidence text, anchor keys promoted | cosine distance |
| `graph_expand` | `kg.traverse` from the top-10 `node_ann` seeds, both directions, bounded hops | `(depth, path_confidence DESC)` |

```
score(node) = SUM over lists it appears in of  1 / (60 + rank_in_that_list)
```

**Why rank-based fusion, not score-based (e.g. weighted cosine + normalized
hop count):** a cosine distance from an HNSW index and a hop-distance from a
graph traversal are not on a comparable scale, and there is no principled way
to normalize them onto one — any weighting you pick is a calibration you
invented, not one you measured. Ranks are comparable by construction: "1st
place in this list" means the same thing regardless of what produced the
list. `k=60` is the standard Cormack constant used throughout the GraphRAG and
IR literature; it damps the advantage of rank 1 over rank 2 (so one list's
noisy top pick doesn't dominate) without needing to be tuned per query.

### 5.3 The `provenance` object and its three consumers

Every `kg.hybrid_search` row returns:

```json
{
  "node_ann": {"rank": 3},
  "graph_expand": {"rank": 1},
  "lists_matched": 2,
  "evidence_strength": 0.9936
}
```

Three things read this, and each would break silently if it were absent:

1. **The Workflow/Engagement Agent's citation requirement.** The prompt
   requires citing which list(s) a claim's grounding came from; a result with
   only `graph_expand` provenance and low `evidence_strength` is a much
   weaker basis for an assertion than one hit by three lists with strong
   evidence, and the model is expected to say so.
2. **The rival grader** (`trn.duel`, `docs/06-training.md`) scores retriever
   variants against exactly this object — recall/precision at each list, not
   just the fused top-k.
3. **The RL reward function** reads it to compute path-groundedness: did the
   model's claim actually appear in something retrieved, and how strongly.

### 5.4 Lexical fallback

`kg.lexical_search` (trigram similarity via `pg_trgm`, `label %> text`) exists
for exact internal jargon an embedding model has never seen — an internal
system code, a ticket-status string, an acronym coined inside the engagement.
It has no `provenance` object (nothing to fuse against); cite the `node_key`
directly. Cheap, always worth running when `kg_search` comes back thin.

---

## 6. pgvector operations

Deployed: **pgvector 0.8.2** on **PostgreSQL 16.14** (confirmed live —
`SELECT extversion FROM pg_extension WHERE extname='vector'`). `db/001`
refuses to apply on anything below **0.8.0**, checked with a `DO` block that
parses `extversion` and raises if it's under `[0,8,0]` — this is a hard
minimum, not a recommendation, because `hnsw.iterative_scan` (0.8.0+) is a
correctness fix, not a speed one (Section 6.3).

### 6.1 Index definitions as shipped

```sql
CREATE INDEX node_emb_hnsw_activity ON kg.node_embedding
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE is_current AND node_type = 'activity';
```

Six node-embedding indexes (`activity`, `process`, `system`+`system_object`,
`control`+`decision`, `pain_point`+`opportunity`, and an `_all` catch-all),
two edge-embedding indexes (`_all`, and `_flow` scoped to
`precedes`/`hands_off_to`/`depends_on`), one chunk index. All storage is
`vector(1024)` (`kg.embedding` domain, `db/001`); every index is an
**expression index** on `embedding::halfvec(1024)`, halving index memory
(2 bytes/dim vs 4) at a small, usually-negligible recall cost from the lower
mantissa precision.

### 6.2 `m` / `ef_construction`, and when to change them

Shipped defaults — `m=16`, `ef_construction=64` — are pgvector's own defaults
and are correct up to roughly 1M vectors. A single engagement holds 10³–10⁵
nodes; the platform stays well inside that band per engagement (indexes are
not shared across engagements' rows in any way that matters for sizing, since
every partial index is additionally scoped by `WHERE ... engagement_id` at
query time even though the index itself is engagement-agnostic).

Raise `ef_construction` to 128 only if recall@10, as measured by the rival
grader (`trn.retriever_variant` / `trn.duel`), sits below target *after*
`ef_search` tuning has been exhausted (Section 6.3) — `ef_construction`
controls build-time graph quality and is markedly more expensive to raise
than `ef_search`, which is a pure runtime knob. Tune the cheap knob first.

### 6.3 `ef_search`, the 40–200 band, and the sequential-scan cliff

`kg.tune_session()` (`db/008`) sets three session GUCs and is called once per
pooled connection by `packages/fde-mcp/src/fde_mcp/db.py`'s `configure` callback:

```sql
CREATE FUNCTION kg.tune_session(p_ef_search int DEFAULT 100,
                                p_iterative text DEFAULT 'relaxed_order',
                                p_max_scan int DEFAULT 20000)
...
  IF p_ef_search < 40 OR p_ef_search > 200 THEN
    RAISE EXCEPTION 'hnsw.ef_search % outside safe band 40..200', p_ef_search;
  END IF;
```

`ef_search` below 40 starts trading away recall faster than latency improves.
Above ~200, the planner's cost model starts abandoning the HNSW index
entirely for a sequential scan on filtered queries — this is a **cliff**, not
a gradual slope: latency observed elsewhere jumping from ~2.5ms to ~365ms at
the crossover, not a smooth curve you can back off from once you notice.
`kg.tune_session` refuses out-of-band values outright rather than clamping
them, and `packages/fde-mcp/src/fde_mcp/db.py` deliberately lets that exception surface at pool-open
time rather than catching it — a misconfigured deployment should fail loudly
at startup, not silently serve degraded retrieval for months.

### 6.4 `hnsw.iterative_scan`: relaxed_order vs strict_order

This is the actual correctness fix pgvector 0.8.0 shipped. Before it, a
filtered ANN query —

```sql
SELECT ... WHERE node_type = 'control' ORDER BY embedding <=> $1 LIMIT 20
```

— fetched a fixed HNSW candidate batch *before* applying the filter. On a
selective filter this silently returns fewer than 20 rows, or zero, even when
20+ matching rows exist elsewhere in the index. It reads as "the graph
doesn't know that," which is indistinguishable from a real gap in the graph
unless you already know to suspect the index.

`kg.tune_session` sets `hnsw.iterative_scan = 'relaxed_order'` as the default
— results come back only approximately distance-ordered, but the shortfall is
gone, and `kg.hybrid_search`'s RRF fusion re-ranks everything anyway, so
losing strict intra-list ordering costs nothing downstream. `strict_order` is
available and preserves exact ordering at additional scan cost; use it only
for a query where a caller consumes the raw `kg.ann_*` output directly
without going through fusion (there is no such call site in this codebase
today — every `kg.ann_*` function is called either directly by `kg_search`'s
fused pipeline or would be if exposed, so `relaxed_order` is the platform-wide
default with no exception carved out).

`hnsw.max_scan_tuples = 20000` bounds how many extra candidates iterative scan
will examine hunting for enough post-filter matches, so a pathological filter
(a node type with almost no live rows) cannot turn one query into an unbounded
scan.

### 6.5 The halfvec cast trap

The index is built on the *expression* `embedding::halfvec(1024)`, not on
`embedding` itself. Postgres only uses an expression index when the query
contains the identical expression:

```sql
-- Uses the index:
ORDER BY ne.embedding::halfvec(1024) <=> $1::halfvec(1024)

-- Silently sequential-scans the whole table -- no error, no warning:
ORDER BY ne.embedding <=> $1
```

There is no plan warning for this — `EXPLAIN` just shows a `Seq Scan` where
you expected an `Index Scan`, and correctness is unaffected (you get the
right answer, slowly). This is exactly why every `kg.ann_*` function lives in
`db/008_retrieval.sql` and nothing hand-writes an ANN query at a call site:
one file gets the cast right, once, and `packages/fde-mcp/src/fde_mcp/server.py` never constructs raw
vector SQL.

### 6.6 Partial indexes per node type as a *true* pre-filter

The six type-scoped indexes in Section 6.1 are not a workaround for the
iterative-scan shortfall — they are the better fix where the query pattern
allows it. A partial index `WHERE node_type = 'activity'` physically contains
only `activity` rows, so a query filtered to `activity` never needs iterative
scan's shortfall-recovery machinery at all; there's no post-filter step,
because there's nothing to filter out. Measured elsewhere at roughly 11x
smaller and 20x faster to build than the catch-all index, for a type covering
about 9% of rows, at equivalent query latency. Use `relaxed_order` iterative
scan for the catch-all index and cross-type queries; rely on the partial
indexes, not iterative scan, whenever the query is scoped to one of the six
covered type groups.

### 6.7 Build memory: a worked calculation

`db/003`'s own comment gives the rule of thumb (4–5x raw vector bytes) and a
worked figure for 100k nodes at 1024 dims stored as `halfvec` (2 bytes/dim):

```
raw vector bytes  = 100,000 rows * 1024 dims * 2 bytes/dim  = 204,800,000 bytes  ~= 195 MB
index working set = 195 MB * ~5x                            ~= 975 MB  ~= ~1 GB

SET maintenance_work_mem = '2GB';           -- headroom above the ~1GB working set
SET max_parallel_maintenance_workers = 7;   -- pgvector 0.8+ parallel HNSW build
```

Undersizing `maintenance_work_mem` doesn't fail the build — it spills to disk
and the build slows by an order of magnitude, which reads as "the migration
is hanging," not as an out-of-memory error. Set it explicitly for any bulk
reindex or initial backfill; do not rely on the server default.

### 6.8 REINDEX after heavy churn

HNSW does not rebalance on delete the way a B-tree does — because `kg.node`
and `kg.edge` are append-only, "delete" here means a row falling out of a
`WHERE is_current` partial index when it's closed out, which is a normal,
continuous background rate, not a bulk operation. The rule of thumb from
pgvector's own operational guidance: once churn (closed-out rows accumulated
against an index's original build size) exceeds roughly 30%, `REINDEX
CONCURRENTLY` rather than let searches keep paying the cost of skipping
tombstoned entries. There's no automated trigger for this in the platform —
track it as an operational metric (Section 8) and reindex during a
maintenance window, `CONCURRENTLY` so retrieval keeps serving from the old
index until the new one is ready.

---

## 7. Traversal

`kg.traverse` (`db/008`) is a recursive CTE using PostgreSQL 14's `CYCLE`
clause:

```sql
) CYCLE nkey SET is_cycle USING cyc_path
```

### 7.1 Three mandatory bounds, and why depth alone fails

```sql
IF p_max_hops > 6   THEN RAISE EXCEPTION 'max_hops % exceeds the hard ceiling of 6', p_max_hops; END IF;
IF p_max_nodes > 5000 THEN RAISE EXCEPTION 'max_nodes % exceeds the hard ceiling of 5000', p_max_nodes; END IF;
```

Plus `edge_types` (an allowlist that prunes branching factor directly). All
three are enforced unconditionally — there is no "unlimited" mode. Depth
alone is insufficient because a recursive CTE re-evaluates its recursive term
against the *entire* working table each iteration with no automatic pruning:
on a graph with a handful of hub nodes (a `system` node that thirty
activities all `depends_on`, say), each additional hop multiplies the
frontier by that hub's fan-out, and three hops from a hub can produce a
frontier in the thousands even though the *path length* bound (`max_hops`)
was perfectly reasonable. `max_nodes` is the bound that actually saves you —
it caps total work regardless of how bushy the graph turns out to be, which
`max_hops` structurally cannot do.

### 7.2 The edge index strategy

`kg.traverse` walks `live_edge`, filtered to `valid_to IS NULL AND tx_to IS
NULL` (current facts) plus an optional `p_as_of`. Two composite partial
indexes make each hop an index scan rather than a table scan:

```sql
CREATE INDEX edge_fwd_idx ON kg.edge (engagement_id, src_key, edge_type, dst_key)
  WHERE valid_to IS NULL AND tx_to IS NULL;
CREATE INDEX edge_rev_idx ON kg.edge (engagement_id, dst_key, edge_type, src_key)
  WHERE valid_to IS NULL AND tx_to IS NULL;
```

Forward and reverse both exist because `p_direction` can be `'out'`, `'in'`,
or `'both'`, and a reverse traversal ("what depends on this system") without
`edge_rev_idx` would force a full scan of `kg.edge` filtered by `dst_key` —
exactly the query `kg.impact_radius` runs on every call. `edge_depends_idx`
additionally dedicated to `depends_on` alone, because dependency-closure
queries are, per `db/002`'s comment, the most-run query pattern in the
platform.

### 7.3 When a closure table would be worth adding

A materialized transitive-closure table (precomputing every reachable pair up
to some depth) trades write-time cost for read-time cost, and is worth it
when reads vastly outnumber writes to the same subgraph and read latency at
the current bound is unacceptable. Neither holds here today: engagement
graphs are actively edited throughout an engagement (every merged proposal
potentially invalidates closure rows touching the changed keys), and the
6-hop/5000-node bounds keep worst-case traversal cost bounded and, in
practice, sub-second. Revisit if a specific query pattern (say, impact-radius
queries against a hub `system` node touched by hundreds of activities) shows
up as a measured latency problem that raising `ef_search`/index tuning cannot
fix — that is a sign the *access pattern*, not the index, is the bottleneck,
and only then does the write-side cost of a closure table start paying for
itself.

---

## 8. Embedding model choice

`kg.embedding` is a fixed `vector(1024)` domain (`db/001`) — changing the
dimension is a migration, not a config flag, because every HNSW index in
`db/003` is built against that exact width.

| | Titan Text Embeddings v2 | Cohere Embed v4 |
|---|---|---|
| Dimensions | 256 / 512 / 1024 | 256 / 512 / 1024 / 1536 |
| Normalization | `normalize` param (bool) | not applicable the same way — outputs are asymmetric by design |
| Output formats | float only | float, int8, binary |
| Context window | 8K tokens | model-card documented, not independently re-verified here (see `docs/99-sources.md`) |
| Batch | none — one `invoke_model` call per text | up to 96 texts/call |
| Asymmetric input types | none (symmetric) | `search_document` (indexed side) vs `search_query` (query side) |

**Recommendation: Titan v2 at 1024 dims, `normalize=true`, as shipped.**
Reasoning:

- **No batch endpoint is a real cost, and it's already paid for.** Titan
  requires one `invoke_model` call per text; `embedder_worker.py` already
  eats this cost as a background async worker off the merge path (Section
  9), so the lack of batching costs latency-to-index-freshness, not
  latency-to-human. Cohere's 96-item batch would meaningfully help a cold
  full-corpus backfill; it doesn't help the platform's actual hot path (one
  node/edge at a time as proposals merge).
- **Titan is symmetric; Cohere is asymmetric by design.** Cohere's
  `search_document`/`search_query` split *can* outperform a symmetric model
  on pure query-vs-document retrieval, but it adds an operational sharp edge:
  get `input_type` backwards on either side and recall quietly drops with no
  error (`packages/fde-mcp/src/fde_mcp/embeddings.py`'s own docstring flags this explicitly). Titan
  has no such failure mode to get wrong, at the cost of not exploiting the
  asymmetry if it would have helped.
- **`packages/fde-mcp/src/fde_mcp/embeddings.py` already threads `input_type` through unconditionally**
  (`kg_search` calls `embed(..., input_type="search_query")`;
  `embedder_worker.py` calls `embed(..., input_type="search_document")`) —
  Titan silently ignores it. This means switching `FDE_EMBED_MODEL_ID` to a
  `cohere.*` model requires **no call-site changes**, only a re-embed
  (Section 9). Titan is the safer default; Cohere is a same-day migration if
  a measured recall gap on the rival-grader benchmark (`docs/06-training.md`)
  justifies the added asymmetric-input-type operational risk.
- **int8/binary output formats** are a real cost lever for corpora far larger
  than a single engagement's 10⁵ nodes, but `kg.embedding`'s fixed
  `vector(1024)` float domain would need its own migration to consume them
  usefully (a binary-quantized column is a different storage type, not a cast)
  — not worth it at this platform's per-engagement scale.

---

## 9. Operational runbook

### 9.1 Re-embedding on a model change

1. Set `FDE_EMBED_MODEL_ID` (and `FDE_EMBED_DIMENSIONS` if it changes — it
   cannot exceed 1024 without a `kg.embedding` domain migration) for
   `packages/fde-mcp/src/fde_mcp/server.py` and every `embedder_worker.py` process.
2. Backfill: `INSERT INTO kg.embed_queue (engagement_id, subject_kind,
   subject_id) SELECT engagement_id, 'node', node_id FROM kg.node_current`
   (and the edge equivalent) for every row you want re-embedded under the new
   model — the worker's `ON CONFLICT (subject_kind, subject_id) DO NOTHING`
   means you can safely re-enqueue everything, including rows already
   embedded under the old model.
3. Run N copies of `embedder_worker.py` concurrently (`FOR UPDATE SKIP
   LOCKED` on the queue makes this safe) until the backlog drains
   (`kg.embed_queue WHERE completed_at IS NULL`).
4. `kg.node_embedding`/`kg.edge_embedding` carry `model_id` per row — you can
   run old and new models side by side during a backfill and query which rows
   are still on the old model at any point; nothing forces a hard cutover.
5. `REINDEX CONCURRENTLY` the affected HNSW indexes once the backfill
   completes and churn (Section 6.8) crosses the 30% line, or immediately if
   the new model's vectors have a meaningfully different distribution (a
   dimension change forces this regardless, since the old vectors are gone).

### 9.2 Adding a node type or edge type

1. Write the migration: `ALTER TYPE kg.node_type ADD VALUE 'new_type'` (or
   the edge equivalent). `ALTER TYPE ... ADD VALUE` cannot run inside the
   same transaction as code that uses the new value, so this is its own
   migration file, applied and committed before anything references it.
2. Update the Engagement Agent's prompt (`packages/fde-agents/src/fde_agents/engagement/prompt.py`) —
   the ontology is stated in full, inline, in the system prompt, not by
   reference to the schema.
3. Decide which HNSW partial index (if any) the new type should get its own
   entry in (Section 6.1) — a new high-volume node type without a partial
   index falls back to the `_all` catch-all, which is correct but slower.
4. If the new type participates in gate policy, add or update the relevant
   `hitl.gate_policy` row (`db/010`) — a new node type with no matching
   policy still gets the catch-all ontology gate, so nothing is unreviewed,
   but a type that should trigger a stronger gate (e.g. a new type of
   control) needs its own policy row.
5. Get a human to review the migration. This step is not optional — it's the
   entire point of Section 1.3.

### 9.3 Reindexing

```sql
REINDEX INDEX CONCURRENTLY kg.node_emb_hnsw_activity;
```

`CONCURRENTLY` so retrieval keeps serving from the existing index until the
rebuild finishes; it takes roughly 2x the disk space of the index being
rebuilt for the duration. Set `maintenance_work_mem`/
`max_parallel_maintenance_workers` per the Section 6.7 calculation before a
bulk reindex across multiple indexes, and reindex the smaller type-scoped
indexes before the `_all` catch-all if doing all of them in one maintenance
window, since the catch-all is the largest and most likely to blow the
memory budget if run concurrently with others.

### 9.4 What to monitor

| Metric | Why | Where |
|---|---|---|
| `kg.embed_queue` backlog depth & age | A stuck embedder means new facts are invisible to `kg_search` even though they're merged | `SELECT count(*), min(enqueued_at) FROM kg.embed_queue WHERE completed_at IS NULL` |
| `kg.embed_queue.attempts` near `MAX_ATTEMPTS` | A verbalisation or Bedrock error that keeps failing the same row | `WHERE attempts >= 4` |
| Index churn since last REINDEX | The 30% line in Section 6.8 | closed-out row count vs index's row count at last build |
| `ef_search`-band violations | Someone bypassed `kg.tune_session`'s guard, or a config drifted | should be structurally impossible given `packages/fde-mcp/src/fde_mcp/db.py`'s pool `configure` callback — alert if it ever happens |
| p50/p95 `kg_search` latency vs the `ef_search=200` cliff | Approaching the sequential-scan cliff before it's hit in production | `trn.trace_step.retrieval->>'latency_ms'` for `tool_name='kg_search'` |
| `evidence_strength` distribution near 0.65 | How often gate policy 6 is firing, and whether the SME quorum burden is calibrated correctly | `kg.evidence` joined against `hitl.proposal_gate` |
