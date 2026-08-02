# The MCP Surface

`mcp/server.py` is the only way any agent touches the database. This
document is the tool contract, the deployment shapes, and the security model
around it. Everything below is read directly from the shipped code
(`mcp/server.py`, `mcp/db.py`, `mcp/embeddings.py`, `mcp/README.md`) — tool
names, argument types, and defaults are exact, not paraphrased.

---

## 1. Design principle: a thin typed wrapper, and why that's non-negotiable

Every tool in `server.py` is one or two SQL statements against functions in
`db/008_retrieval.sql`, `db/004_hitl_gates.sql`, `db/006_workflows.sql`, and
`db/007_drift.sql`. No retrieval, gating, or faithfulness logic is
reimplemented in Python.

The reason this matters is specific to training, not just "clean
architecture." `docs/01-knowledge-graph.md` and `db/008`'s own header say it
directly: production inference and RL rollout must see byte-identical
retrieval behaviour, or you are training a policy against an environment it
will not be deployed into. If `server.py` re-implemented even a piece of
`kg.hybrid_search`'s fusion logic in Python — say, a slightly different RRF
constant, or a Python-side re-ranking step the SQL function doesn't have —
then a rollout worker calling the same tool through a slightly different code
path could see different results than production, and the policy would learn
to exploit whatever the rollout path happens to do, not what it will actually
be served in production. Keeping every retrieval, gating, and faithfulness
decision inside `db/008`'s functions and calling them identically from both
paths (`db/012_rl_rollout_role.sql`'s `fde_rl_rollout` role has `EXECUTE` on
every function in `kg`, same as `fde_agent`) is what makes that guarantee
actually hold, rather than being an aspiration in a comment.

---

## 2. Tool reference

18 tools total: 9 read, 3 proposal, 3 drift, 3 workflow (`mcp/README.md`).

### 2.1 Read tools

#### `kg_head_commit(engagement_id: str) -> dict`

Returns the current sealed HEAD commit: `commit_id`, `commit_uuid`, `title`,
`content_digest`, `sealed_by`, `sealed_at`, `created_at`, plus `has_head:
bool`. **Call this first, every session** — pin `commit_id` for the rest of
the task (cite it as `base_commit_id` in proposals, `pinned_commit_id` in
workflow drafts). If the engagement has never been merged into, `has_head` is
`false` and `commit_id` is `null` — there's nothing to query yet.

*Failure mode:* none beyond the generic DB error boundary (Section 5) — this
tool cannot itself be called with bad arguments beyond a malformed
`engagement_id` UUID.

#### `kg_as_of(engagement_id: str, at: str) -> dict`

Point-in-time snapshot for ISO-8601 `at`, diffed against current HEAD. Runs
`kg.node_as_of`/`kg.edge_as_of` and `kg.node_current`/`kg.edge_current`, then
computes set differences in Python. Returns counts at `at`, counts at head,
and `diff_vs_head` (added/removed node and edge keys, capped at 200 each with
separate total counts). `at` is used as **both** the valid-time and
transaction-time cutoff — "the graph's current best belief about what was
true at `at`," not "what we believed at `at`." For the latter question, this
tool does not help; query `kg.node_as_of` directly with a transaction-time
cutoff in the past.

*When to use:* "what changed since X" or "what did the graph say on X."

*Failure mode:* a malformed timestamp raises from `_parse_timestamp` before
any query runs — a plain Python exception, not a DB error, so it is not
caught by `_pg_error_boundary` and propagates as-is.

#### `kg_search(engagement_id, query: str, k: int = 20 [1-200], node_types: list[NodeType] | None, expand_hops: int = 2 [0-6]) -> dict`

The primary retrieval tool. Embeds `query` (`input_type="search_query"`),
calls `kg.hybrid_search`, returns up to `k` nodes with `rrf_score` and
`provenance`. `seed_k` is derived internally as `max(30, k)`, not exposed as
an argument.

*When to use:* any open-ended "what/who/how" question about the business.

*Failure mode:* comes back thin or empty on exact internal jargon an
embedding model has never seen (ticket codes, internal acronyms) — the
docstring explicitly directs the model to `kg_lexical_search` in that case,
not to conclude the graph doesn't have the answer.

#### `kg_lexical_search(engagement_id, text: str, k: int = 10 [1-100]) -> dict`

Trigram similarity over node labels (`kg.lexical_search`, `pg_trgm`). No
`provenance` object — nothing to fuse against; cite `node_key` directly.

*When to use:* exact jargon matching, as a `kg_search` fallback or
complement.

*Failure mode:* matches on label similarity only, not semantics — will not
find a conceptually related but differently-worded node. Do not treat an
empty result as "this concept doesn't exist in the graph."

#### `kg_get_node(engagement_id, node_key: str) -> dict`

Full context on one node: the node row, incoming and outgoing live edges
grouped by `edge_type` (each with neighbor key/label, edge label/attributes/
confidence/`human_confirmed`), evidence rows joined to their sources, and
aggregate `evidence_strength`.

*When to use:* "tell me everything about X," after `kg_search`/`kg_traverse`
has surfaced a candidate `node_key` — not for broad discovery.

*Failure mode:* `{"error": "node not found", "hint": "..."}` if the key
doesn't exist or isn't currently live (retired/superseded) — this is a
structured tool result, not a raised exception, so the model must check for
`error` in the response rather than assuming success.

#### `kg_traverse(engagement_id, start_keys: list[str], edge_types: list[EdgeType] | None, max_hops: int = 3 [1-6], max_nodes: int = 500 [1-5000], direction: "out"|"in"|"both" = "out") -> dict`

Bounded, cycle-safe multi-hop walk (`kg.traverse`). Both this tool's own
Pydantic field bounds *and* the database function's own `RAISE EXCEPTION`
enforce the ceilings — belt and suspenders, since a client that bypasses
schema validation (a hand-rolled JSON-RPC call, a fuzzing rollout policy)
still hits the DB-side check. Returns each reachable node once, at its
shortest hop-count and highest-confidence path.

*When to use:* understand a neighbourhood; there is no unbounded mode by
design — if you need more, traverse again from the returned frontier.

*Failure mode:* `max_hops > 6` or `max_nodes > 5000` is rejected by Pydantic
before the DB is even touched (a validation error, not a `RuntimeError` from
the SQL layer).

#### `kg_dependency_closure(engagement_id, node_key: str, max_hops: int = 4 [1-6]) -> dict`

"What does this depend on, transitively?" Follows
`depends_on`/`consumes`/`recorded_in`/`gated_by` **outward**
(`kg.dependency_closure`, itself `kg.traverse` with that fixed edge-type
list, direction `'out'`).

*When to use:* "what breaks if I automate/remove X," from the dependency
side. For the reverse question, use `kg_impact_radius`.

#### `kg_impact_radius(engagement_id, node_key: str, max_hops: int = 4 [1-6]) -> dict`

Reverse of the above: follows the same edge types plus
`precedes`/`hands_off_to`, **inward** (`direction='in'`).

*When to use:* before proposing a `retire_node`/`retire_edge`, or scoping the
blast radius of a proposed automation.

#### `kg_process_flow(engagement_id, process_key: str) -> dict`

Ordered activity sequence for a process (`kg.process_flow`) — topologically
ordered by in-degree over `precedes` within the process's activity set, with
`performed_by`, `gated_by`, `records_to`, `next_keys` per activity.

*When to use:* explain or diagram an end-to-end process. Use `kg_traverse`
instead if you need edges beyond one process's own activity set.

### 2.2 Write-proposal tools

#### `kg_propose(engagement_id, title, rationale, items: list[ProposalItem], base_commit_id: int, trace_session_id: str | None) -> dict`

**Does not write `kg.node`/`kg.edge` and does not submit.** Creates a
`hitl.proposal` in `draft` status plus its `hitl.proposal_item` rows. Returns
`proposal_id` and a **preview** of `required_gates` (live
`hitl.compute_required_gates` output — not yet frozen; recomputed and frozen
at submit time).

`ProposalItem` (Pydantic model, mirrors `hitl.proposal_item`):

| Field | Type | Notes |
|---|---|---|
| `op` | `Literal["add_node","update_node","retire_node","add_edge","update_edge","retire_edge"]` | |
| `node_type` | `NodeType \| None` | set iff `op` ends in `node` |
| `edge_type` | `EdgeType \| None` | set iff `op` ends in `edge` |
| `subject_key` | `str` | node_key or edge_key being created/changed |
| `payload` | `dict` | full desired end state; ignored for `retire_*` |
| `supersedes_key` | `str \| None` | populated by dedup search |
| `source_ids` | `list[int]` | **required non-empty** for every `add_*`/`update_*` item |
| `agent_confidence` | `float` [0.0-1.0] | |

The node/edge type discipline (`op` implies exactly one of `node_type`/
`edge_type`) is enforced twice: Pydantic doesn't cross-check it at all (both
can technically be passed), but the database's `item_type_matches_op` CHECK
constraint does, and a mismatch surfaces as that check-violation message
verbatim.

*Failure mode:* nothing fails here that would fail at submit — this tool
accepts unsourced items (submit is where that's checked). A proposal built
here with mismatched `op`/`node_type`/`edge_type` fails at the `INSERT`
inside this same tool call, with the CHECK-violation message.

#### `kg_submit_proposal(proposal_id: int) -> dict`

Runs `hitl.submit_proposal`: validates every non-retire item has ≥1 evidence
source, computes and **freezes** the gate set (`hitl.proposal_gate` rows with
due dates), moves the proposal to `submitted`. Never partially submits — the
underlying function does everything in one transaction and raises rather than
returning a partial result.

*Failure mode (the important one):* if validation fails — unsourced items, or
(fail-closed) no gate policy matched at all — the database `RAISE EXCEPTION`s
and this tool surfaces the message **verbatim** (see Section 6). The model is
expected to read the message, fix the proposal (usually: call `kg_propose`
again with `source_ids` populated), and resubmit.

#### `kg_proposal_status(proposal_id: int) -> dict`

Per-gate breakdown: quorum, distinct authorised non-superseded `approve`
decisions (respecting `allow_self` and `reviewer.is_active`), the raw
decision list, and `still_needed_reviewers` — the eligible reviewers who
haven't yet approved. `overall_satisfied` mirrors `hitl.gates_satisfied`.

*When to use:* "who's still blocking this," for reporting back to a human.

*Failure mode:* `{"error": "proposal not found", "hint": "check proposal_id"}`
— structured, not raised.

### 2.3 Drift tools

#### `drift_scan(engagement_id: str) -> dict`

Runs `sor.run_all_detectors` — refreshes `sor.observed_transition`
`CONCURRENTLY`, then all four detectors. Returns counts of new/updated
signals per detector. **Detection itself is SQL, not a model judgement** —
this tool only triggers the run.

*When to use:* a scheduled monitor run, not a tight loop — can be slow on a
large observation table.

*Failure mode -- verified against the live schema, and worth being precise
about because there are two distinct issues layered here:*

1. **`fde_agent` (the role every MCP tool call runs as) has no `EXECUTE`
   grant on `sor.run_all_detectors` at all.** `db/010` grants `EXECUTE` on
   that function to `fde_ingest` only; there is no blanket `GRANT EXECUTE ON
   ALL FUNCTIONS IN SCHEMA sor TO fde_agent` the way there is for `kg`, and
   `db/011`'s supplemental grants don't add it either. Confirmed live: `SET
   LOCAL ROLE fde_agent; SELECT sor.run_all_detectors(...)` raises
   `permission denied for function run_all_detectors`. This means
   `drift_scan`, called through the MCP server as shipped, **always** hits
   this `InsufficientPrivilege` before it ever reaches the matview refresh.
   It surfaces as a structured `{"error": ..., "hint": "a DB grant is
   missing for this operation under the current role..."}` (the generic
   grant-missing hint, since the error message doesn't mention "materialized
   view" and so doesn't trigger `_hint_for`'s more specific branch) — the
   server does not crash, per `test_server.py`'s
   `test_drift_scan_fails_gracefully_not_loudly`, but the tool is not
   currently reachable as deployed. The fix is a one-line grant
   (`GRANT EXECUTE ON FUNCTION sor.run_all_detectors(uuid) TO fde_agent;`),
   not a schema-ownership change.
2. **Separately, and already fixed:** the `REFRESH MATERIALIZED VIEW
   CONCURRENTLY` step inside the function requires the *calling role* to own
   the matview on PostgreSQL 16 (no grantable `REFRESH` privilege
   pre-PG17). `sor.run_all_detectors` is `SECURITY DEFINER` specifically to
   close this (Section 8) — verified live: calling it as `fde_ingest` (which
   does have `EXECUTE`) against the deployed schema (pgvector 0.8.2 / PG
   16.14) succeeds and returns a real per-detector count JSON. **`mcp/
   README.md`'s "Known limitations" section, which describes this as broken
   "for ANY role, including fde_ingest," is stale** — it predates (or was
   never updated after) the `SECURITY DEFINER` fix now present in
   `db/007_drift.sql`; do not rely on that section as written.

Net: today, `drift_scan` fails for `fde_agent` specifically because of (1),
the missing `EXECUTE` grant — a real, narrow gap, distinct from the
already-solved ownership problem in (2) that the stale README text still
attributes the failure to.

#### `drift_list(engagement_id, state: DriftState = "open", min_severity: DriftSeverity = "medium") -> dict`

Lists `sor.drift_signal` rows at or above `min_severity` (order: `info` <
`low` < `medium` < `high` < `critical`), most severe and most recent first.
Read `detail` before triaging — it carries expected-vs-observed, sample size,
effect size.

#### `drift_triage(engagement_id, signal_id: int, state: Literal["open","triaged","proposal_raised","accepted","dismissed"], note: str) -> dict`

Annotates a signal. **`state='resolved'` is not in the `Literal` — Pydantic
rejects the call before it reaches the database.** This is enforced at the
tool-schema layer because the grant that lets `fde_agent` write these columns
(`db/011`) is column-scoped, not value-scoped — Postgres has no per-value
grant, so "only a human resolves a signal" has to be enforced in
`server.py`, not by a database privilege.

*Failure mode:* `{"error": "drift signal not found for this engagement", ...}`
if `signal_id`/`engagement_id` don't match a row — structured, not raised.

### 2.4 Workflow tools

#### `wf_list(engagement_id, status: WorkflowStatus = "published") -> dict`

Lists workflows in a given status. Default `"published"` — the ones product
ops actually runs.

#### `wf_get(workflow_id: int) -> dict`

Full workflow: header, ordered steps with their bindings, and
`commits_behind` (how many commits ahead HEAD is of `pinned_commit_id`; `0`
if current, `null` if the engagement somehow has no sealed commit at all,
which the docstring notes "cannot normally happen for a published workflow").
A nonzero `commits_behind` does not by itself mean broken — cross-check
`drift_list` for a `stale_pin` signal naming this workflow.

#### `wf_draft(engagement_id, slug, title, root_process_key, pinned_commit_id: int, steps: list[StepSpec]) -> dict`

Authors a **draft** workflow with steps and bindings, then calls
`wf.assert_faithful` inside the same transaction. If faithfulness fails (an
unbound non-notify step, or a binding to a key not live at
`pinned_commit_id`), the whole draft **rolls back** — nothing is left
half-created — and the tool re-raises the message verbatim.

`StepSpec` mirrors `wf.step` plus a `bindings: list[StepBindingSpec]`
(`subject_kind: "node"|"edge"`, `subject_key`, `relation`). Version is
auto-incremented per `slug` so re-drafting the same slug never collides.

*Never publishes* — the agent role has `INSERT` but no `UPDATE` grant on
`wf.workflow`/`wf.step` (Section 8), so publication is structurally
unreachable from this tool regardless of what the model intends.

*Failure mode:* the faithfulness-violation message names the specific unbound
step keys or dangling binding keys — fix those (add a `StepBindingSpec`, or
bind to a live key) and call again.

---

## 3. Deployment shape A: in-runtime MCP

`FDE_MCP_TRANSPORT=http` runs `server.py` as streamable-HTTP on
`0.0.0.0:8080` at path `/mcp` — this is the exact shape AgentCore Runtime
requires when a runtime is declared with `protocol=MCP`: the runtime
health-checks and proxies directly to that host/port/path inside the
container. The server runs as a sidecar/same-container process next to the
agent loop, no separate Gateway hop. Session correlation uses the
`Mcp-Session-Id` header (not `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`,
which is a distinct AgentCore-level session concept — see
`docs/00-architecture.md` and `docs/99-sources.md` for the sessions
reference).

**When this is right:** a single dedicated agent, local development, and (per
`mcp/README.md`) this platform's own test suite. Simpler operationally — no
Gateway to provision — at the cost of one deployed copy of the MCP server per
agent runtime rather than one shared copy.

`FDE_MCP_TRANSPORT=stdio` (the default) is the same server over stdin/stdout
as a subprocess, for local dev with an MCP Inspector or Claude-Desktop-style
client, or for an in-process Gateway target that spawns it as a subprocess.

---

## 4. Deployment shape B: behind AgentCore Gateway

`agents/deploy/create_gateway.py` provisions a Gateway with `protocolType=
'MCP'` and:

```python
protocolConfiguration={
    "mcp": {
        "searchType": "SEMANTIC",
        "supportedVersions": ["2025-06-18"],
        "instructions": "...",
    }
}
```

Two target types registered against it:

1. **`mcpServer` target** — points at the deployed `server.py`
   streamable-HTTP endpoint (same binary as Shape A, just fronted). This is
   the day-to-day `kg_*`/proposal/drift/workflow surface.
2. **`lambda` target** — an escape hatch for tools that must run as a Lambda
   rather than tie up the MCP server's own connection pool
   (`FDE_DB_POOL_MAX`) for a long-running batch job — e.g. a full
   `sor.observation` backfill from a customer data warehouse export.

`searchType='SEMANTIC'` is what lets the Gateway's own tool search (the
built-in `x_amz_bedrock_agentcore_search` tool) narrow an agent's visible
tool list by semantic relevance to the current task, rather than requiring
every tool name to be hard-coded into the agent's own tool list. This is
useful headroom for when the platform's tool surface grows past what fits
comfortably in one system prompt's tool section — at 18 tools today, semantic
search is not load-bearing, but the Gateway is provisioned with it on from
the start so growth doesn't require a re-provision.

**When this is right:** multiple agent runtimes (Engagement, Workflow,
Development) sharing one deployed copy of the graph tool surface with
centralized auth, rather than each needing its own direct network path to the
MCP server container. This is the platform's actual production shape — three
runtimes, one Gateway, one `mcpServer` target.

**Honest comparison:** Shape A is simpler and has one fewer network hop and
one fewer thing to provision/monitor; it does not compose across multiple
consuming runtimes without deploying N copies of the same server. Shape B
adds Gateway provisioning, auth configuration, and a hop, in exchange for a
single shared deployment and centralized rate limiting/allow-listing. Pick A
for a single-agent deployment or local dev; pick B the moment a second
consuming runtime exists, which for this platform is immediately (three
runtimes from day one).

### Gateway specifics

- **Auth:** `authorizerType='CUSTOM_JWT'` with `customJWTAuthorizer.{
  discoveryUrl, allowedAudience, allowedClients}` — an OIDC discovery URL
  (Cognito user pool `.well-known/openid-configuration` in this platform's
  deployment scripts), not a static API key.
- **Transport:** streamable-HTTP only, per AgentCore Gateway's own protocol
  contract.
- **URL shape / bearer auth:** callers present a bearer token validated
  against the JWT authorizer configuration above; the Gateway itself issues
  no credentials.
- **Relevant limits** (see `docs/99-sources.md` for sourcing): 100 targets
  per gateway, 1000 tools per target, 6MB tool payload, 15-minute gateway
  timeout. At one `mcpServer` target (18 tools) plus one `lambda` target (1
  tool today), this platform is nowhere near any of these ceilings; they
  matter if the batch-Lambda escape hatch grows into many narrow tools rather
  than staying a small, deliberately underused hatch.

---

## 5. Session and tracing

`FDE_TRACE_SESSION_ID`, when set, drives every tool call to append one
`trn.trace_step` row: `role='tool'`, `trainable=false`, `tool_result`
truncated to 8KB (`TRACE_RESULT_MAX_BYTES`), and for the retrieval tools a
populated `retrieval` JSON (`k`, `returned`, `rrf_top`, `hops`, `latency_ms`).

`trainable=false` on every tool turn is not a default that happens to be
correct — it is the mechanism that prevents training on retrieved tokens,
which `db/009_training.sql`'s own comment calls "the single most common cause
of a retrieval-trained model that hallucinates plausible-looking evidence
instead of calling the tool." The SFT collator and the RL rollout both key
loss-masking off this column; getting it backwards for even one tool call
type would silently teach the model to imitate tool *output* rather than
learn to *call* the tool.

The insert runs in a **SAVEPOINT** nested inside the tool's own transaction
(`_emit_trace`, `async with conn.transaction()`), so a tracing failure — most
commonly, `FDE_TRACE_SESSION_ID` pointing at a session row that doesn't exist
yet — never rolls back the tool's actual work. It's logged at `warning` and
swallowed. Note the ordering inside `_emit_trace`: `_jsonify` runs **before**
`_truncate_for_trace`, because psycopg hands back native `uuid.UUID`/
`datetime` objects that `Jsonb()`'s `json.dumps` would otherwise choke on —
getting this backwards silently breaks tracing for exactly the tool results
that contain ids and timestamps, i.e. almost all of them, and the RL/SFT
substrate ends up quietly empty.

`server.py` does **not** create the `trn.trace_session` row — that's expected
to happen before the first tool call, from whatever orchestrates the agent
run (`agents/common/tracing.py`'s `start_session`, which pins `base_commit_id`
from `kg_head_commit` at that moment).

The `retrieval` JSON populated per tool: `kg_search` reports `rrf_top` (the
top fused score, or `null` if no results); `kg_lexical_search`,
`kg_traverse`, `kg_dependency_closure`, `kg_impact_radius` report `k`/
`returned`/`hops`/`latency_ms` with `rrf_top: null` (no fusion happened).
This is what `trn.failure_label`'s automated classifiers and the RL reward
function read to compute retrieval-quality signals per turn.

---

## 6. Connection pooling and `kg.tune_session` on checkout

`mcp/db.py` calls `kg.tune_session(KG_EF_SEARCH, KG_ITERATIVE_SCAN,
KG_MAX_SCAN_TUPLES)` once per **new physical connection**, via the
`AsyncConnectionPool`'s `configure` callback — not once per tool call. This
is a correctness requirement, not a performance tweak, for exactly the reason
`docs/01-knowledge-graph.md` Section 6.4 documents: an untuned session (no
`hnsw.iterative_scan = 'relaxed_order'`) can silently return fewer rows than
requested from a filtered `ORDER BY embedding <=> q LIMIT k` — pgvector's
iterative-scan shortfall. If tuning only happened per-tool-call rather than
per-connection, a pooled connection reused across many tool calls could serve
some calls tuned and others not depending on exactly when in the pool's
lifecycle they landed; binding it to `configure` guarantees every connection
the pool ever hands out is tuned before it serves its first query.

`kg.tune_session`'s own out-of-band guard (`RAISE EXCEPTION` outside
`ef_search` 40–200) is deliberately allowed to propagate at pool-open time
rather than being caught — a misconfigured `KG_EF_SEARCH` environment
variable fails the deployment at startup, loudly, instead of quietly serving
degraded retrieval indefinitely.

---

## 7. Error contract

Every tool is wrapped by `_pg_error_boundary`. The classification:

| Error class | Examples | Surfaced as |
|---|---|---|
| Self-correctable | `RaiseException` (plpgsql `RAISE EXCEPTION`, SQLSTATE P0001), `CheckViolation`, `NotNullViolation`, `ForeignKeyViolation`, `UniqueViolation`, `ExclusionViolation`, `InvalidTextRepresentation`, `InvalidParameterValue`, `DatatypeMismatch` | Re-raised as a plain `RuntimeError` with the DB message **verbatim** |
| Everything else | connection drop, `InsufficientPrivilege`, `OperationalError`, `QueryCanceled`, `UndefinedFunction`/`UndefinedTable`, unexpected server errors | `{"error": "...", "hint": "..."}` |

The distinction is about what the model can do with the failure. A `RAISE
EXCEPTION 'proposal % has % item(s) with no evidence source'` is something
the model caused and can fix by changing its own next call — surfacing it
verbatim lets the model self-correct without a round trip through a human. An
`InsufficientPrivilege` or a dropped connection is not something the model's
arguments caused and not something it can fix by trying a different argument
— returning it as a structured `{error, hint}` keeps a single flaky query
from tearing down the whole tool-call loop, and the `hint` field
(`_hint_for`) gives a specific, actionable diagnosis rather than a generic
"something went wrong" (see the `InsufficientPrivilege` / matview-ownership
example in Section 8, and the `QueryCanceled` → "narrow k/max_hops/max_nodes
and retry" hint for a timed-out query).

Getting this split wrong in either direction breaks model self-correction:
surfacing a permission error as if it were fixable by different arguments
sends the model into a retry loop that can never succeed; swallowing a
genuine input-validation `RAISE` into a generic `{error, hint}` hides the
specific, fixable reason from the model that actually caused it.

---

## 8. Security model

Every tool call opens exactly one transaction (`db.tool_transaction()`, the
only sanctioned way to get a connection in this codebase — nothing calls
`pool.connection()` directly) and, inside it:

1. `SET LOCAL statement_timeout = '20s'` (`FDE_STATEMENT_TIMEOUT`) — the
   platform's synchronous request/response budget for a tool call.
   Long-running work (embedder backfills, out-of-band detector runs) does not
   go through this path.
2. `SET LOCAL ROLE fde_agent` (`db.set_role`, identifier interpolated via
   `psycopg.sql.Identifier`, never string-formatted).

Both `SET LOCAL`s are transaction-scoped and evaporate on commit or rollback
regardless of outcome — a connection handed back to the pool is always back
at the "owner-adjacent" principal with the default `statement_timeout`. The
process's actual DB credential (resolved from `FDE_DB_DSN` /
`FDE_DB_SECRET_ARN` / IAM auth) is only ever a *ceiling*; `SET LOCAL ROLE` is
the real security boundary Postgres enforces for the duration of each call.

**What `fde_agent` structurally cannot do** (`db/010`, reinforced by
`db/011`):

- No `INSERT`/`UPDATE`/`DELETE` on `kg.node`, `kg.edge`, or `kg.commit` —
  explicitly `REVOKE`d. The only path to a graph change is `kg_propose` →
  `kg_submit_proposal` → (human gate) → `hitl.merge_proposal`, and
  `merge_proposal` itself is not even reachable — `EXECUTE` is `REVOKE`d from
  `fde_agent` in `db/010`.
- No `UPDATE` on `wf.workflow`/`wf.step` — `wf_draft` gets `INSERT` only
  (`db/011`), so a drafted workflow can never be flipped to `published` or
  edited after creation from this role, regardless of what the tool's Python
  code intends to do.
- No path to `sor.drift_signal.state = 'resolved'` — Postgres has no
  per-value grant, so this is enforced in `drift_triage`'s `Literal` type
  (Section 2.3), not by the database.
- No `UPDATE` on `hitl.proposal_gate.cleared_at`, `trn.trace_session.outcome`/
  `label_proposal_id`/`label_source`/`split` — the training *label* can only
  be set by the gate service or `fde_training`, never by the agent whose work
  is being labelled. `db/011`'s own comment calls this "the single most
  important privilege boundary in the training pipeline."

`sor.run_all_detectors` (Section 2.3's `drift_scan`) is `SECURITY DEFINER`,
owned by the migration owner — the one place in the tool surface where a
function deliberately runs with elevated privileges rather than the calling
role's own grants. This is narrowly scoped to the one operation
(`REFRESH MATERIALIZED VIEW CONCURRENTLY`) that has no grantable privilege
short of ownership on PostgreSQL 16, and `REVOKE ALL ... FROM PUBLIC` plus an
explicit `GRANT EXECUTE` to only `fde_ingest` keeps the elevation from
becoming a general-purpose escalation path — nothing else in the function
does anything `fde_ingest` couldn't already do with its own grants. As shipped
this `EXECUTE` grant is **not** extended to `fde_agent` (Section 2.3), which
is why `drift_scan` fails when called through the MCP server today; granting
it is the correct fix and does not weaken this boundary, since the function
body performs no write the drift-triage tool surface doesn't already permit.
