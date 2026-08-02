# fde-training

Offline training pipeline: SFT export, reward functions, GRPO rollout
environment, rival graders, and the Bedrock RFT path. Everything here runs
outside the agent containers — the heavy ML stack lives behind the optional
`train` extra so it can never reach a runtime image (`uv sync --package
fde-training --extra train`).

This file is the package README that module docstrings cite (`common.py`,
`sft_config.py`, `lambda_grader.py`). Blueprint-level rationale lives in
`docs/06-training.md`; this file stays at the "which file, which command,
which environment" level.

## Pipeline map

| Module | Role |
|---|---|
| `export_sft.py` | gate outcomes → trainable traces (accepted / corrected / rejected splits) |
| `sft_config.py` | `TrainingProfile`, TRL `SFTConfig` / LoRA builders, two-phase `verify_masking` |
| `train.py` | `train-sft` and `train-grpo --mode {qa,env}` command bodies (lazy heavy imports) |
| `rewards/` | the 8-term composite reward (`DEFAULT_WEIGHTS` sums to 1.0) + reward-hacking monitor |
| `rollout_env.py` | self-managed RL environment; same SQL as the MCP server, commit-pinned |
| `rival_grader.py` | pairwise retrieval tournaments, Bradley-Terry leaderboard, judge calibration |
| `generate_traces.py` | rejection-sampled teacher traces over seeded eval queries |
| `seed_eval.py` | loads `fixtures/eval_queries.jsonl` into `trn.eval_query` |
| `bedrock_rft.py` | RFT dataset builder + grader Lambda packaging/deploy |
| `lambda_grader.py` | the stdlib-only RFT grader Lambda (zipped alone, see below) |

CLI surface (`uv run fde-training --help`): `export-sft`, `rewards-test`,
`verify-masking`, `seed-eval-queries`, `rival-grader run|calibrate|leaderboard`,
`generate-traces`, `train-sft`, `train-grpo`, `rft-submit
build-dataset|build-job-config|package-grader|deploy-grader`. Retrieval-touching
commands take `--embed {bedrock,fake}`; the fake embedder is never a silent
default for tournament or RFT-dataset retrieval.

## DSN contract

`resolve_dsn()` uses `FDE_DB_DSN` if set, else local peer-auth against
`FDE_DB_NAME` (default `fde`). That local posture — a developer Postgres with
`db/rebuild.sh` applied — is exactly what this package was developed and
tested against. For IAM-authenticated RDS, mint the token in a wrapper script
and export `FDE_DB_DSN`; this package deliberately does not re-implement
token minting. All DB access issues `SET LOCAL ROLE fde_training` per
transaction, mirroring `fde_mcp.db.tool_transaction`'s security contract
(`fde_rl_rollout` for rollout episodes).

Note: `fixtures/eval_queries.jsonl` is a repo fixture, not packaged into the
wheel — `seed-eval-queries` defaults resolve only from a source checkout.

## Volume gates

Condensed from `docs/06-training.md` §8 — the triggers below gate each stage;
starting a stage early optimises against noise:

| Stage | Trigger | Do |
|---|---|---|
| 0 | day one | prompt + retrieval tuning; stand up the rival grader |
| 1 | ≥200 traces | tune retrieval against the leaderboard |
| 2 | ≥350 accepted traces | SFT LoRA (`train-sft`) — tool syntax + traversal shape |
| 3 | SFT plateaued, ~2.4k examples, named failures | GRPO (`train-grpo`) on the live env |
| 4 | ≥8k, RL stable | scale RL |

Most teams should stop after stage 2. A judge is usable as an RL reward only
in the calibrated band (Cohen's kappa ≥ 0.78, ≤ 0.82 before "suspiciously
high", position-bias rate < 0.15) — enforced in code by
`rival_grader.usable_as_rl_reward` and pinned by `test_docs_sync.py`.
`max_seq_length=8192` in `TrainingProfile` exists because multi-turn tool
traces run long; do not lower it without re-checking trace-length percentiles
in `export-sft --stats`.

## Comparison table: self-managed RL vs Bedrock RFT

`lambda_grader.py` deliberately re-implements the structural parts of the
composite reward with stdlib only. This duplication is the accepted cost of
the fully-managed path:

| | Self-managed (`rollout_env.py` + `train-grpo`) | Bedrock RFT (`bedrock_rft.py` + `lambda_grader.py`) |
|---|---|---|
| Retrieval during rollout | live `kg.*` SQL per step, commit-pinned | none — grader sees only the dataset row's gold/provenance fields |
| Reward | full 8-term composite over the transcript | 3 structural terms (grounded 0.45 / outcome 0.40 / citation 0.15) |
| Infra you run | GPU box + Postgres | nothing (Lambda + managed rollouts) |
| Latency budget | yours | hard: grader must return in seconds, no DB round-trip |
| Packaging | `[train]` extra | `rft-submit package-grader` — the module zipped **alone**, zero non-stdlib imports |
| When to prefer | multi-turn tool-use training, when-to-stop behaviours | single-turn grounding/format shaping without infra |

## Test plan

`uv run pytest packages/fde-training` — pure tests always run; `requires_db`
cases run when `FDE_DB_DSN` points at a migrated database; heavy-dep cases
(`test_train.py`, `test_verify_masking.py` token phase) import-skip unless the
`train` extra is installed (CI runs them in a dedicated job; locally use an
isolated venv — syncing the extra into the shared workspace venv removes
sibling packages' dependencies).

Notable suites: `test_parity.py` pins rollout-env SQL byte-equal to the MCP
server's and asserts the exact expected divergences (`kg_traverse` extra
args, `kg_get_node` shape, as-of overloads), so silent convergence and new
drift both fail; `test_rollout_env_asof.py` proves closure/radius honour the
pinned commit; `test_docs_sync.py` keeps `docs/06`'s weight table equal to
`DEFAULT_WEIGHTS` and the kappa thresholds present in docs; the tiny
committed tokenizer under `tests/fixtures/tiny_tokenizer/` keeps everything
hermetic (no Hub downloads).
