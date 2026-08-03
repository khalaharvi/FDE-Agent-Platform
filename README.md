# FDE Agent Platform

[![CI](https://github.com/khalaharvi/FDE-Agent-Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/khalaharvi/FDE-Agent-Platform/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](pyproject.toml)
[![Docs](https://img.shields.io/badge/docs-fde--agent--platform-6f42c1.svg)](https://fde-agent-platform.mintlify.app)

Three forward-deployed-engineering agents on Amazon Bedrock AgentCore, over a
knowledge graph in PostgreSQL, with deterministic human gates between the
agents and the graph.

> **The invariant: agents propose. Humans dispose. Only `hitl.merge_proposal`
> writes the graph** — enforced at four independent layers and asserted by
> twenty-five privilege-denial checks in CI.

**Who this is for.** Forward-deployed and solutions engineers rebuilding the
same engagement scaffolding for every client; platform leads who must answer
"what can the agent write, and who approved it?" — the answer here is a
`SELECT`, not a model output; and practitioners here for one extractable idea,
human gates in pure SQL. **Not for you** if you want a general agent framework
— if you don't need governed writes to shared state, you don't need this.

![The review console: a proposal moves from the queue through two SQL-computed gates into a sealed commit](docs-site/images/console.gif)

The review console on a demo-seeded database (`./db/rebuild.sh fde --with-demo`).
Neither gate there was a model's decision — `hitl.compute_required_gates()`
computed both — and the merge re-checked their quorum inside its own
transaction. Walk it yourself:
[your first merge](https://fde-agent-platform.mintlify.app/first-merge).

```mermaid
flowchart LR
    SME([SMEs, documents]) --> EA[Engagement Agent]
    EA -- evidenced proposals --> G{{"Human gates<br/>(pure SQL, fail-closed)"}}
    G -- the only write path --> KG[("Knowledge graph<br/>Postgres + pgvector")]
    KG -- pinned commits --> WA[Workflow Agent]
    KG -- pinned commits --> DA[Development Agent]
    WA -- workflows, drift triage --> G
    DA -- agent packages + evals --> G
    SOR[(Systems of record)] -- adapters --> DET[SQL drift detectors] --> WA
    G -. every human decision .-> T[Training labels → SFT / RL]
```

**How it works:** the Engagement Agent turns what SMEs say into evidenced,
structured claims. Claims are proposals, not writes — pure SQL computes which
humans must sign off, and nothing reaches the graph until they do. The
Workflow Agent reads the graph at a pinned commit and authors workflows whose
every step must name the graph element it implements, then watches the system
of record for drift. The Development Agent turns published workflows into
deployable agent packages with evals derived from the workflow itself. Every
human decision becomes a training label, which is what makes the system
improve rather than just run.

---

## Quick start (clone → running locally, no AWS account needed)

```bash
# toolchain
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-packages --frozen

# database (CI's exact pgvector image, port 55432)
docker compose up -d db
export PGHOST=localhost PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres
./db/rebuild.sh fde --with-demo   # drop --with-demo if you want an empty graph
export FDE_DB_DSN="postgresql://postgres:postgres@localhost:55432/fde"

# prove it works, then open the review console
uv run pytest packages
FDE_GATE_DEV_PRINCIPAL=sme@example.com uv run fde-gate-dev   # → http://127.0.0.1:8787/ui
```

`./db/rebuild.sh` runs `dropdb`, `createdb`, and `psql` on your machine, so the
Postgres **client tools** have to be on `PATH` even when the server itself is in
Docker: `brew install libpq` (then add its `bin` to `PATH`) or
`apt-get install postgresql-client`. Without them the first command fails with
`command not found: dropdb`.

Already running Postgres with pgvector ≥ 0.8? Skip compose:
`./db/rebuild.sh fde --with-demo && export FDE_DB_DSN=postgresql:///fde`.

<details>
<summary><b>Beyond hello world: MCP server, pipelines, deploy</b></summary>

```bash
FDE_MCP_TRANSPORT=stdio uv run fde-mcp     # the MCP server, 21 tools
uv run fde-training export-sft --stats     # training data from gate outcomes
uv run fde-sor replay --help               # drift ingestion from a JSONL export

# deploy (AWS credentials; pick a model preset below)
uv run fde-agents-deploy codezip --agents engagement --code-bucket <bucket>
uv run fde-agents-deploy runtimes --agents engagement --role-arn <arn> \
  --artifact-mode code --model-preset balanced

# ARM64 container path
docker buildx build --platform linux/arm64 --build-arg AGENT=engagement \
  -f packages/fde-agents/Dockerfile -t $ECR/fde-engagement:$TAG --push .
```

Lint, types, and the full gate list are in [CONTRIBUTING.md](CONTRIBUTING.md).

</details>

> **Two non-negotiable prerequisites** (only if you skip the compose file):
> pgvector must be **≥ 0.8.0** — `db/001` refuses to install below it, because
> older versions silently return incomplete filtered ANN results. And build
> pgvector with `OPTFLAGS=""` — the default `-march=native` produces a binary
> that crashes Postgres with **SIGILL** on a different CPU. This repo hit both.

---

## Choosing models (the budget dial)

Model choice is the dominant variable cost, so it is configuration, not code:
`FDE_MODEL_PRESET` (or `fde-agents-deploy runtimes --model-preset`) selects a
tier, and an explicit `FDE_MODEL_ID` always wins. Presets live in one place —
`packages/fde-agents/src/fde_agents/common/config.py` (`MODEL_PRESETS`).

| Preset | Engagement / Workflow | Development | ~Inference cost* |
|---|---|---|---|
| `premium` (default) | Claude Sonnet 5 | Claude Sonnet 5 | ~$350/mo |
| `balanced` | GLM-5 | Claude Sonnet 5 | ~$150/mo |
| `budget` | GLM-5 | GLM-5 | ~$110/mo |

Cheap models are unusually safe here: every agent write passes the same human
gates regardless of which model proposed it — sloppiness lands in a review
queue, not in the graph, and each reviewer correction becomes SFT training
data. Two rules the presets encode: small non-agentic models never drive the
21-tool loop, and a cheaper *judge* is a separate, measured decision —
`fde-training rival-grader calibrate` must show Cohen's kappa ≥ 0.78 before
any judge's verdicts are trusted as an RL reward.

<details>
<summary><b>Cost math and cheaper dials</b></summary>

*Order-of-magnitude, one active engagement (~200 sessions × ~500k tokens),
Bedrock on-demand list prices (Sonnet 5 $3/$15 per MTok, GLM-5 $1/$3.20),
before prompt caching (−75% on cached reads) and batch-tier discounts.
Sonnet 5's tokenizer produces ~30% more tokens for the same text than
Sonnet 4.5's — re-baseline token budgets when comparing. GLM-4.7
($0.60/$2.20) and GLM-4.7-Flash ($0.07/$0.40) remain on Bedrock if you want
to dial a custom `FDE_MODEL_ID` lower. Re-derive against current pricing for
a real budget.

</details>

<details>
<summary><b>Bring your own model provider (Anthropic API, OpenAI, Gemini, any /v1)</b></summary>

```bash
uv run fde-providers login anthropic          # validates live, stores in the OS keychain
export FDE_MODEL_PROVIDER=anthropic FDE_MODEL_ID=claude-sonnet-5
export FDE_EMBED_PROVIDER=openai FDE_EMBED_MODEL_ID=text-embedding-3-small
uv run fde-agents-local doctor                # 5 preflight checks
uv run fde-agents-local engagement --task ingest_interview \
  --engagement-id <id> --input '{"material": "..."}'
```

Zero AWS credentials required. `FDE_MODEL_ID` is required for any non-Bedrock
provider; env vars override the keychain for CI/servers. Anthropic and
OpenCode Zen have no embeddings API — pair chat and embedding providers
deliberately. Full provider matrix, login UX, and honesty labeling in
[`docs/12-providers.md`](docs/12-providers.md).

</details>

---

## What is here

<details>
<summary><b>Repository layout and quality gates</b></summary>

```
fde-platform/
├── pyproject.toml       uv workspace root: 5 members, ruff + mypy config
├── uv.lock              committed; every build is --frozen
├── docs/                14 documents — the blueprints
├── db/                  17 migrations + a 25-test smoke suite
├── packages/
│   ├── fde-mcp/         MCP server: 21 tools (graph, proposals, drift, workflow, evidence), embedder worker
│   ├── fde-agents/      3 AgentCore runtimes over one shared common/runtime.py, + deploy CLI
│   ├── fde-training/    SFT export + trainers, rewards/ package, rollout env, rival graders, RFT path
│   ├── fde-gate/        gate service Lambda: review console, evidence intake, merge, workflow publish + runner
│   └── fde-sor/         SoR adapters (rest_poll/event_stream/db_cdc/replay), drift-scan, backfill
├── infra/k8s/           EKS manifests for the fde-sor jobs (kagent-compatible)
├── diagrams/            6 self-contained HTML diagrams, light + dark
└── .github/workflows/   CI: static → database → tests → train-tests → ARM64 images → gated deploy
```

**Gates, all green:** `ruff check` (174 files) + `ruff format --check` (172) ·
`mypy --strict` on the four production packages (88 files, 0 issues) ·
`uv lock --check` · 25 SQL smoke tests on a clean rebuild · 740 Python tests
against live Postgres · twenty-five privilege-denial invariants, all correctly denied.

</details>

<details>
<summary><b>The invariant, in depth</b></summary>

Enforced at four independent layers, so removing any one does not open the door:

1. **Grant** — `fde_agent` has no write privilege on `kg.node`/`kg.edge`/`kg.commit`
2. **Function** — `hitl.merge_proposal` is `SECURITY DEFINER`, revoked from `PUBLIC`
3. **Precondition** — gate quorum is re-checked *inside* the merge transaction
4. **Fail-closed submit** — a proposal matching no policy raises rather than sailing through

The gate set required to merge is computed by `hitl.compute_required_gates()` —
pure SQL over a policy table. Same proposal, same policy, same gates, every
time. No model in that decision. That buys an auditable answer to "why did
this need compliance sign-off," an agent that cannot negotiate its way to a
lighter review, and a training label that is genuinely human, not the model
grading itself.

![A merged proposal in the console: both gates cleared, with the reviewer, the comment, and how long they looked recorded against each one](docs-site/images/console-merged.png)

That is the whole audit trail for one merge — which gates were required, who
cleared each, what they said, and the commit it sealed — and none of it is
reconstructed after the fact.

</details>

<details>
<summary><b>Design decisions worth arguing with</b></summary>

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

</details>

<details>
<summary><b>Diagrams</b> (self-contained HTML, light + dark, no network required)</summary>

| File | Shows |
|---|---|
| `diagrams/01-system-topology.html` | AWS deployment topology and trust boundaries |
| `diagrams/02-graph-schema.html` | ERD, bitemporal columns, partial HNSW indexes |
| `diagrams/03-ontology.html` | all 15 node + 15 edge types, with a Quote-to-Cash example |
| `diagrams/04-hitl-flow.html` | the gate flow with all four fail-closed points |
| `diagrams/05-drift-loop.html` | reality drift vs pin drift, and how each closes |
| `diagrams/06-training-loop.html` | gate outcomes → labels → SFT → GRPO → graders |

</details>

---

## Read the docs in this order

Start with three: [`docs/00-architecture.md`](docs/00-architecture.md) (how it
fits together), [`docs/07-hitl-gates.md`](docs/07-hitl-gates.md) (**the gate
model — the one to read if you read only one**), and
[`docs/09-deployment.md`](docs/09-deployment.md) (AgentCore, IAM, CI/CD, cost).

<details>
<summary><b>Full reading list (14 documents)</b></summary>

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
| 12 | [`docs/12-providers.md`](docs/12-providers.md) | bring-your-own model provider: login, provider matrix, local full-flow walkthrough |
| — | [`docs/99-sources.md`](docs/99-sources.md) | every external claim → a URL, plus what could not be verified |

</details>

---

## Status and honesty

| Claim | Evidence |
|---|---|
| 17 migrations apply cleanly on an empty database | `./db/rebuild.sh` — the CI gate |
| 25 end-to-end smoke tests pass | incl. fail-closed submit, unauthorised approval, review→edit→merge→label, workflow publish/run/human-response, observation dedup, as-of traversal |
| 740 Python tests, 730 of them in one run against live Postgres | fde-mcp 110 · fde-agents 117 · fde-training 183 · fde-gate 160 · fde-sor 170. The 10 that skip need the `train` extra's heavy deps (9) or `wal_level=logical` (1); CI installs the extra and re-runs 39 of the fde-training tests in a job of its own |
| 25 privilege-denial invariants hold | asserted in CI, not trusted |
| 6 diagrams screenshot-verified | both colour schemes |

**Not validated here:** anything requiring live AWS credentials — Bedrock
model and embedding calls, AgentCore control-plane and data-plane calls,
Gateway and Memory provisioning, and Bedrock RFT submission. Those are
written against the verified API shapes documented in `docs/99-sources.md`
but have not been executed.

**Fourteen real bugs were found and fixed while building this**, recorded
candidly in `docs/99-sources.md` §7–§8. Highlights: `merge_proposal` stamped
`valid_from` with `clock_timestamp()` while reads used transaction time (a
read-your-own-write failure); the same function later aborted on its own
stale-pin housekeeping the second time an engagement merged; the one role
meant to write training labels couldn't reach the `trn` schema; a reward
function paid an RL policy 0.85 for doing nothing; retrieval tournaments
silently ranked noise from fake embeddings; and pgvector compiled with the
default `-march=native` crashed Postgres with **SIGILL** when the host CPU
changed. The unverifiable AWS claims (cold-start latency, RDS pgvector
minors, Aurora + HNSW memory, the evaluator roster, CMI-as-RFT-base) are
listed explicitly in `docs/99-sources.md` §6 — check those before you build
a release process around them.

---

Apache-2.0 · [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Changelog](CHANGELOG.md)
