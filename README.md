# FDE Agent Platform

Three forward-deployed-engineering agents on Amazon Bedrock AgentCore, over a
knowledge graph in PostgreSQL, with deterministic human gates between the agents and
the graph.

Built from the *3 Pillars of the Agent* framing — Engagement, Workflow, Development —
as a system an engineer can build and deploy, not a slide.

---

## The idea in five sentences

An **Engagement Agent** sits with SMEs and turns what they say into evidenced,
structured claims about how work is done. Those claims are **proposals**, not writes:
a deterministic SQL function computes which humans must sign off, and only after they
do does anything reach the graph. A **Workflow Agent** reads the graph at a pinned
commit and authors workflows where every step must name the graph element it
implements — then watches the system of record and reports where reality has diverged
from the model. A **Development Agent** turns a published workflow into a deployable
agent package with evals and guardrails derived from the workflow itself. Every human
decision along the way is captured as a training label, which is what makes the whole
thing improve rather than just run.

---

## What is here

```
fde-platform/
├── pyproject.toml       uv workspace root: 5 members, ruff + mypy config
├── uv.lock              committed; every build is --frozen
├── docs/                13 documents — the blueprints. START HERE.
├── db/                  15 migrations + a 25-test smoke suite
├── packages/
│   ├── fde-mcp/         MCP server: 21 tools (graph, proposals, drift, workflow, evidence), embedder worker
│   ├── fde-agents/      3 AgentCore runtimes over one shared common/runtime.py, + deploy CLI (codezip/runtimes/gateway/memory)
│   ├── fde-training/    SFT export + trainers, rewards/ package, rollout env, rival graders, RFT path
│   ├── fde-gate/        gate service Lambda: review console, merge, workflow publish + runner
│   └── fde-sor/         SoR adapters (rest_poll/event_stream/db_cdc/replay), drift-scan, backfill
├── infra/k8s/           EKS manifests for the fde-sor jobs (kagent-compatible)
├── diagrams/            6 self-contained HTML diagrams, light + dark
└── .github/workflows/   CI: static → database → tests → train-tests → ARM64 images → gated deploy
```

**Gates, all green:** `ruff check` + `ruff format --check` across 152 files ·
`mypy --strict` on `fde-mcp`, `fde-agents`, `fde-gate`, `fde-sor` (78 source
files, 0 issues) · `uv lock --check` · 25 SQL smoke tests on a clean rebuild ·
**545 Python tests against live Postgres** (555 collected; the 10 skips are
heavy-ML and CDC gates with their own CI job / marker) · eight database
invariant attempts, all correctly denied.

---

## Read in this order

| # | Document | What it answers |
|---|---|---|
| 0 | [`docs/00-architecture.md`](docs/00-architecture.md) | how it fits together, and why relational+pgvector over a graph DB |
| 1 | [`docs/01-knowledge-graph.md`](docs/01-knowledge-graph.md) | the ontology, bitemporality, retrieval, pgvector operations |
| 2 | [`docs/07-hitl-gates.md`](docs/07-hitl-gates.md) | **the gate model — read this one if you read only one** |
| 3 | [`docs/02-agent-engagement.md`](docs/02-agent-engagement.md) | Engagement Agent blueprint |
| 4 | [`docs/03-agent-workflow.md`](docs/03-agent-workflow.md) | Workflow Agent blueprint |
| 5 | [`docs/04-agent-development.md`](docs/04-agent-development.md) | Development Agent blueprint |
| 6 | [`docs/05-mcp-surface.md`](docs/05-mcp-surface.md) | every tool contract |
| 7 | [`docs/06-training.md`](docs/06-training.md) | SFT → RL → rival graders, with honest volume gates |
| 8 | [`docs/08-drift-monitor.md`](docs/08-drift-monitor.md) | SoR adapters and the four detectors |
| 9 | [`docs/09-deployment.md`](docs/09-deployment.md) | AgentCore, IAM, CI/CD, cost, build order |
| 10 | [`docs/10-prodops-runbook.md`](docs/10-prodops-runbook.md) | for the product operations group |
| 11 | [`docs/11-python-conventions.md`](docs/11-python-conventions.md) | uv workspace, package boundaries, logging, what the tooling enforces |
| — | [`docs/99-sources.md`](docs/99-sources.md) | every external claim → a URL, plus what could not be verified |

---

## The invariant

> **Agents propose. Humans dispose. Only `hitl.merge_proposal` writes the graph.**

Enforced at four independent layers, so removing any one does not open the door:

1. **Grant** — `fde_agent` has no write privilege on `kg.node`/`kg.edge`/`kg.commit`
2. **Function** — `hitl.merge_proposal` is `SECURITY DEFINER`, revoked from `PUBLIC`
3. **Precondition** — gate quorum is re-checked *inside* the merge transaction
4. **Fail-closed submit** — a proposal matching no policy raises rather than sailing through

Smoke tests 2, 3, and 4 exist to prove layers 2–4 hold, including the case where a
reviewer approves a gate they have no authority to clear.

### Why "deterministic"

The gate set required to merge a proposal is computed by
`hitl.compute_required_gates()` — pure SQL over a policy table. Same proposal, same
policy, same gates, every time. No model in that decision.

That buys three things: an auditable answer to "why did this need compliance
sign-off," an agent that cannot negotiate its way to a lighter review, and — the one
that matters most downstream — a training label that is genuinely human, not the
model grading itself.

---

## Quick start (clone → running locally, no AWS account needed)

Everything through step 4 runs entirely on your machine — the database, all
545 tests, the MCP server, the training pipeline, and the drift detectors.
AWS enters only at step 5.

```bash
# 0. Toolchain (uv is the only prerequisite; it manages Python itself)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-packages --frozen

# 1. Database — the same pgvector image CI uses, on port 55432 so it never
#    collides with a Postgres you already run
docker compose up -d db
export PGHOST=localhost PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres
./db/rebuild.sh fde                     # 15 migrations + 25 smoke tests
export FDE_DB_DSN="postgresql://postgres:postgres@localhost:55432/fde"
#    (already running Postgres with pgvector >= 0.8? skip compose:
#     createdb fde && ./db/rebuild.sh fde && export FDE_DB_DSN=postgresql:///fde)

# 2. Prove it works
uv run pytest packages                  # 545 tests against live Postgres
uv run ruff check packages && uv run mypy   # strict, all four production packages

# 3. Run the MCP server locally over stdio
FDE_MCP_TRANSPORT=stdio uv run fde-mcp

# 4. Local, AWS-free pipelines
uv run fde-training export-sft --stats                 # training data from gate outcomes
uv run fde-sor replay --help                           # drift ingestion from a JSONL export
uv run fde-training seed-eval-queries --dry-run \
  --file packages/fde-training/fixtures/eval_queries.jsonl

# 4.5 The product itself: the review console, in your browser, no AWS —
#     the same lambda_handler the deployed console runs (queue is empty
#     until a reviewer is registered and an agent proposes; see docs/10)
FDE_GATE_DEV_PRINCIPAL=sme@example.com uv run fde-gate-dev   # -> http://127.0.0.1:8787/ui

# 5. Deploy (needs AWS credentials; pick a model tier first — see below)
uv run fde-agents-deploy codezip --agents engagement --code-bucket <bucket>
uv run fde-agents-deploy runtimes --agents engagement --role-arn <arn> \
  --artifact-mode code --model-preset balanced

# 6. Build an image (ARM64 is mandatory for the container path)
docker buildx build --platform linux/arm64 --build-arg AGENT=engagement \
  -f packages/fde-agents/Dockerfile -t $ECR/fde-engagement:$TAG --push .
```

## Choosing models (the budget dial)

Model choice is the dominant variable cost, so it is configuration, not
code: `FDE_MODEL_PRESET` (or `fde-agents-deploy runtimes --model-preset`)
selects a tier, and an explicit `FDE_MODEL_ID` always wins over any preset.
Presets are defined in one place —
`packages/fde-agents/src/fde_agents/common/config.py` (`MODEL_PRESETS`).

| Preset | Engagement / Workflow | Development | ~Inference cost* |
|---|---|---|---|
| `premium` (default) | Claude Sonnet 5 | Claude Sonnet 5 | ~$350/mo |
| `balanced` | GLM-5 | Claude Sonnet 5 | ~$150/mo |
| `budget` | GLM-5 | GLM-5 | ~$110/mo |

*Order-of-magnitude, one active engagement (~200 sessions × ~500k tokens),
Bedrock on-demand list prices (Sonnet 5 $3/$15 per MTok, GLM-5 $1/$3.20),
before prompt caching (−75% on cached reads) and batch-tier discounts.
Sonnet 5's tokenizer produces ~30% more tokens for the same text than
Sonnet 4.5's — re-baseline token budgets when comparing. GLM-4.7
($0.60/$2.20) and GLM-4.7-Flash ($0.07/$0.40) remain on Bedrock if you want
to dial a custom `FDE_MODEL_ID` lower. Re-derive against current pricing
for a real budget.

Why cheap models are unusually safe here: every agent write passes the same
human gates and fail-closed validation regardless of which model proposed
it — sloppiness lands in a review queue, not in the graph, and each
reviewer correction becomes SFT training data. Two rules the presets
encode: small non-agentic models never drive the 21-tool loop (route them
to triage/formatting only), and a cheaper *judge* is a separate, measured
decision — `fde-training rival-grader calibrate` must show Cohen's kappa
≥ 0.78 before any judge's verdicts are trusted as an RL reward.

**Two prerequisites that are not negotiable.** pgvector **≥ 0.8.0**. `db/001` refuses to
install below it. On older versions, filtered ANN queries silently return incomplete
results — a correctness bug that presents as "the graph doesn't know that."
And build pgvector with `OPTFLAGS=""`: the default `-march=native` produces a
binary tuned to the build host, and Postgres dies with **SIGILL** the first time
an HNSW query runs on a different CPU. This repo hit it.

---

## Design decisions worth arguing with

Each of these is defended at length in the docs. The short version:

**Relational edge tables + pgvector, not Apache AGE or Neptune.** AGE is not
available on RDS or Aurora. Neptune means a second datastore, and `merge_proposal`
needs graph rows, evidence, the embed queue, and a drift signal in one transaction.
Recursive CTEs with the PG14 `CYCLE` clause and partial composite indexes are
adequate at 10³–10⁵ nodes. → `docs/00-architecture.md` §4

**Three embedding granularities, fused with RRF at k=60.** Nodes, edges, and evidence
chunks are embedded separately because queries match at different levels — the edge
*"Sales Rep hands off Quote Approval to Deal Desk when discount exceeds 20%"* is a
sentence worth embedding, and it is invisible if you only embed node summaries. RRF
is rank-based because cosine distance and hop distance are not on a comparable scale.
→ `docs/01-knowledge-graph.md` §5

**Drift detection is SQL, not an LLM.** Deterministic, reproducible, cheap, and
honest about sample size. The agent does not decide *whether* drift exists; it
decides what to do about it. → `docs/08-drift-monitor.md`

**Workflows pin to a commit and every step must bind to a graph element.**
`wf.assert_faithful()` refuses to pass a workflow with an unbound step or a binding
to a key that was not live at the pin. That is what makes "faithful" checkable rather
than aspirational. → `docs/03-agent-workflow.md` §2

**Most teams should stop after SFT.** RL costs weeks and its ceiling is set by your
reward function, which is set by your judge, which needs Cohen's kappa ≥ 0.78 against
humans to be trustworthy as a reward at all. → `docs/06-training.md` §8

**The reviewer edit is the most valuable event in the system.** It is a paired
(wrong, right) example on identical input — a free preference dataset from someone
doing their job properly. Design the review UI to make editing as easy as approving.
→ `docs/07-hitl-gates.md` §5

---

## Diagrams

Self-contained HTML, light and dark mode, no network required.

| File | Shows |
|---|---|
| `diagrams/01-system-topology.html` | AWS deployment topology and trust boundaries |
| `diagrams/02-graph-schema.html` | ERD, bitemporal columns, partial HNSW indexes |
| `diagrams/03-ontology.html` | all 15 node + 15 edge types, with a Quote-to-Cash example |
| `diagrams/04-hitl-flow.html` | the gate flow with all four fail-closed points |
| `diagrams/05-drift-loop.html` | reality drift vs pin drift, and how each closes |
| `diagrams/06-training-loop.html` | gate outcomes → labels → SFT → GRPO → graders |

---

## Status and honesty

**Validated here:** all 15 migrations apply cleanly in order on an empty database;
25 end-to-end smoke tests pass (including the fail-closed and unauthorised-approval
cases, the review→edit→merge→label chain, workflow publish/run/human-response,
chunk-grant boundaries, observation dedup, and as-of traversal); 545 Python tests
pass against live Postgres (fde-mcp 66 · fde-agents 92 · fde-training 174 ·
fde-gate 44 · fde-sor 169; the training suite reaches 183 with the `train` extra
installed, exercised in its own CI job); eight privilege-denial invariants hold;
all six diagrams screenshot-verified in both colour schemes.

**Not validated here:** anything requiring live AWS credentials — Bedrock model and
embedding calls, AgentCore control-plane and data-plane calls, Gateway and Memory
provisioning, and Bedrock RFT submission. Those are written against the verified API
shapes documented in `docs/99-sources.md` but have not been executed.

**Four real bugs were found and fixed while building this**, all recorded in
`docs/99-sources.md` §7 — and the completion pass that added the gate service,
the SoR adapters, and the trainers found ten more (a merge that aborted on its
own drift housekeeping the second time it ran, a schema-USAGE grant gap on the
one role meant to write training labels, a reward function that paid an RL
policy 0.85 for doing nothing, tournaments silently ranking retrieval over
fake embeddings, and more), all recorded with the same candour in
`docs/99-sources.md` §8:

- `merge_proposal` stamped `valid_from` with `clock_timestamp()` while reads used
  transaction time — a read-your-own-write failure where `kg.traverse` silently
  returned zero rows.
- `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires the calling role to own the view;
  `sor.run_all_detectors` is `SECURITY DEFINER` for this reason.
- `fde_agent` was never granted `EXECUTE` on `sor.run_all_detectors`, so the
  autonomous drift loop was structurally monitoring nothing — and failing silently
  inside the tool's error boundary.
- pgvector compiled with the default `-march=native` crashed Postgres with **SIGILL**
  after the host CPU changed. Build with `OPTFLAGS=""` on any heterogeneous fleet.

Items that could not be verified from primary AWS sources — cold-start latency,
per-minor pgvector versions on RDS 13–16, Aurora Serverless v2 + HNSW memory
behaviour, the full built-in evaluator roster, and whether a CMI-imported model can
be a Bedrock RFT base — are listed explicitly in `docs/99-sources.md` §6. Check those
before you build a release process around them.
