# Changelog

All notable changes to this repo. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the project is pre-release, so everything currently lives under 0.1.0 and Unreleased.
The migration story is rebuild-from-scratch (`./db/rebuild.sh`, no migration
tracking table) until the first production deployment freezes the schema.

## [Unreleased]

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
- Docs retargeted from the pre-workspace flat layout to `packages/*` paths;
  counts regenerated from measured reality; LoRA doc numbers aligned to the
  shipped defaults (r=16, α=32); kappa RL gate unified at κ ≥ 0.78.

## [0.1.0] — 2026-08-01

Baseline: the original three-package platform (fde-mcp, fde-agents,
fde-training), migrations 001–012, 16 smoke tests, 190 tests, and the 13
blueprint documents. Snapshotted as git commit `4c21adb`.
