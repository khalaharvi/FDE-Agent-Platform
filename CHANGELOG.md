# Changelog

All notable changes to this repo. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the project is pre-1.0, so minor releases may include breaking changes — see `RELEASING.md` for how a release gets cut.
The migration story is rebuild-from-scratch (`./db/rebuild.sh`, no migration
tracking table) until the first production deployment freezes the schema.

## [Unreleased]

### Added

- **Transcript intake in the console** (`/ui/sources`,
  `db/017_console_evidence_intake.sql`). An operator can paste or upload an
  interview or SOP (`.txt` / `.md`) and see, before anything is saved, how it
  will be split and how much of it retrieval will actually be able to reach.
  Ingestion previously meant hand-written chunked JSON over MCP, which put
  the start of the funnel out of reach of the person who conducts the
  interview.
- **The anchor guardrail.** `kg.hybrid_search` keeps only chunks with at
  least one anchor (`db/008_retrieval.sql:317`), so an unanchored ingestion
  has always been storable, embeddable and permanently unsearchable. That
  fact now has a number on it — anchor coverage on every source list and
  detail page — and a 0% source renders a banner naming the consequence and
  the next step instead of a warning that died in an MCP response. A
  **Re-ingest dark chunks** action carries the still-invisible passages into
  a new source version once the nodes they evidence exist. Only dark chunks
  are ever carried forward, so a passage is anchored in at most one version
  and retrieval cannot double-count.
- Five new privilege denials (twenty-five total): `fde_gate_service` gains
  INSERT — and only INSERT — on `kg.source`, `kg.chunk` and
  `kg.embed_queue`, and CI asserts it still cannot UPDATE or DELETE a chunk,
  rewrite a source, or touch `kg.node`. Evidence is propose-side; the graph
  is still written only by `hitl.merge_proposal`.
- `multipart/form-data` support in the gate's request parsing, for the file
  upload. A part that is not valid UTF-8 is refused by name rather than
  stored as replacement characters.
- **Reviewer and authority administration in the console** (`/ui/reviewers`,
  `db/016_reviewer_admin.sql`). Onboarding a reviewer, granting or revoking a
  gate authority, and deactivating someone who has left were the one
  operational task that still required an engineer with database access.
  The page is gated on a new platform-wide `admin` authority
  (`hitl.reviewer_admin`), checked server-side on every GET and POST; the
  first admin on a deployment is still granted by hand, once, and
  `docs/10-prodops-runbook.md` §8 carries the statement.
- Twelve new privilege denials (twenty total): no role but `fde_gate_service`
  may write the reviewer tables, and even it may not DELETE a reviewer or
  rewrite a `principal` — gate decisions reference the row, so both would
  rewrite the audit trail. Its UPDATE is column-scoped to the columns the
  console actually sets.

### Fixed

- Request-parse failures no longer escape `lambda_handler`. Parsing runs
  before any route is matched, so it sits outside the router's error
  contract: a rejected upload or a malformed JSON body left the handler as an
  exception and reached the caller as a bare 502 (a dropped connection on the
  dev server), losing the message that named what to do. Pre-existing for
  malformed JSON; the upload refusals would have inherited it.

### Changed

- Deactivation, never deletion, is now a property of the schema rather than a
  convention: no role holds DELETE on `hitl.reviewer`,
  `hitl.reviewer_authority` or `hitl.reviewer_admin`.

## [0.2.0] — 2026-08-02

First public release: the completion build. Everything below landed between
the 0.1.0 baseline snapshot and the repo going public on GitHub. Also fixes
the two failures the first public CI run surfaced (editable workspace
installs breaking the two-stage Docker images; a hardware-validating
`GRPOConfig` test on CPU-only runners — `docs/99-sources.md` §7.3).

### Added
- **Open-source readiness**: `LICENSE` (Apache-2.0) + `NOTICE`, `SECURITY.md`
  (vuln reporting scoped to the four-layer invariant), `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, issue/PR templates, per-package license metadata.
- **Model presets — the budget dial**: `FDE_MODEL_PRESET` ∈
  {`premium`, `balanced`, `budget`} maps each agent to a model tier
  (`MODEL_PRESETS` in `fde_agents/common/config.py`); explicit
  `FDE_MODEL_ID` always wins; `fde-agents-deploy runtimes --model-preset`
  bakes the resolution into each runtime's env so the proposal audit trail
  records exactly the model that authored it. Cost table in README
  "Choosing models".
- **Local-first onboarding**: `docker-compose.yml` (CI's exact pgvector
  image, port 55432) and `fde-gate-dev` — the review console served
  locally through the same `lambda_handler` the deployed Lambda runs
  (principal synthesized from `FDE_GATE_DEV_PRINCIPAL`; refuses to start
  anonymous). README quick start rewritten as the clone-and-run journey.
- **Gate service + prod-ops console** (`packages/fde-gate`): Lambda + API Gateway
  review queue, proposal decisions/edits/merge, workflow publish + runner with
  human steps in the same queue, deploy CLI. The first way to call
  `hitl.merge_proposal` that isn't `psql`.
- **SoR adapters + drift input** (`packages/fde-sor`): `rest_poll`, `event_stream`
  (SQS), `db_cdc` (wal2json), and replay adapters feeding `sor.observation`
  idempotently; drift-scan closure; backfill Lambda behind the Gateway target;
  serverless (EventBridge/SQS) and EKS (`infra/k8s/`) run targets.
- **Evidence ingestion** (`fde-mcp`): `kg_register_source`, `kg_ingest_chunks`,
  `kg_list_sources` tools (18 → 21) and the embedder worker's chunk branch —
  the third retrieval granularity is live end to end.
- **Trainers** (`fde-training`): `train-sft` (TRL SFTTrainer) and
  `train-grpo --mode {qa,env}`; eval-query seeding (`seed-eval-queries` +
  committed fixture); RFT grader Lambda packaging/deploy; SQL parity tests;
  token-level `verify_masking`.
- Migrations 013–015 (review/run state machines in SQL, observation dedup,
  as-of traversal overloads) and smoke tests 17–25.
- CI: `train-tests` job, gated `deploy` job + `tests/smoke_deployed.py`,
  two new privilege denials (eight total), `fde-sor` image build.
- Package READMEs for `fde-mcp` and `fde-training`; `CLAUDE.md`; this file.

### Fixed
- Twenty audited defects (B1–B20) — the full ledger with root causes lives in
  `docs/99-sources.md` §8. Highlights: `hitl.merge_proposal` aborted on its own
  stale-pin housekeeping on the second merge; `fde_gate_service` couldn't reach
  the `trn` schema it labels; tournaments and RFT datasets silently retrieved
  with fake embeddings; an empty RL transcript scored 0.85; no role could
  publish a workflow at all.
- DX pass: `fde-mcp --help` / `fde-embedder --help` print the env contract
  instead of starting (and crashing) a server; the embedder fails fast on
  configuration errors instead of retrying identical tracebacks forever;
  `fde-sor` and `fde-training` print operator-fixable errors as one line
  instead of stack traces.

### Changed
- **Model refresh (2026-08)**: default agent model → Claude Sonnet 5
  (`anthropic.claude-sonnet-5`, date-less current-generation Bedrock id;
  note the new tokenizer counts ~30% more tokens for the same text);
  `balanced`/`budget` presets → GLM-5 (`zai.glm-5`, Bedrock March 2026);
  rival-grader judge and teacher defaults → Sonnet 5, replacing
  `anthropic.claude-3-5-sonnet-20241022-v2:0`, which AWS retired in
  October 2025 (the old default would 404 on first live use).
- Docs retargeted from the pre-workspace flat layout to `packages/*` paths;
  counts regenerated from measured reality; LoRA doc numbers aligned to the
  shipped defaults (r=16, α=32); kappa RL gate unified at κ ≥ 0.78.

## [0.1.0] — 2026-08-01

Baseline: the original three-package platform (fde-mcp, fde-agents,
fde-training), migrations 001–012, 16 smoke tests, 190 tests, and the 13
blueprint documents. Snapshotted as git commit `4c21adb`.
