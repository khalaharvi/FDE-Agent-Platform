# CLAUDE.md

FDE Agent Platform: three AgentCore agents over a Postgres knowledge graph
with deterministic human gates. Read `README.md` first, then the docs in the
order its table gives. `docs/11-python-conventions.md` is binding for all
Python; `docs/99-sources.md` §7–§8 is the ledger of every bug found so far —
check it before "fixing" something that was already reconciled.

## Commands

```bash
uv sync --all-packages --frozen          # never plain `uv lock` casually; lock is CI-checked
createdb fde && ./db/rebuild.sh fde      # 18 migrations + 25 smoke tests, rebuilds from scratch
FDE_DB_DSN=postgresql:///fde uv run pytest packages   # 814 tests; without DSN the db-marked ones skip
uv run ruff check packages && uv run ruff format --check packages
uv run mypy                              # strict; covers fde-mcp, fde-agents, fde-gate, fde-sor
uv run fde-providers login <provider>    # then: fde-agents-local <agent> --task ... (docs/12)
```

## The invariant (do not weaken)

Agents propose, humans dispose; only `hitl.merge_proposal` writes the graph.
CI asserts twenty-nine privilege denials (`.github/workflows/ci.yml`, "core
invariants must hold"). Any new grant or SECURITY DEFINER function must keep
all twenty-nine failing and must `REVOKE ALL ... FROM PUBLIC` (functions default to
PUBLIC EXECUTE). New tables in a new migration get NO grants automatically.

## Workspace map

| Package | Role | DB role it runs as |
|---|---|---|
| fde-mcp | MCP server (22 tools) + embedder worker | `fde_agent` / `fde_ingest` |
| fde-agents | 3 AgentCore runtimes + deploy CLI | (tools arrive over MCP) |
| fde-training | offline training pipeline (NOT mypy-strict, by policy) | `fde_training` / `fde_rl_rollout` |
| fde-gate | Lambda gate service: review console, evidence intake, merge, wf publish/run | `fde_gate_service` / `fde_prodops` |
| fde-sor | SoR adapters, drift scan, backfill | `fde_ingest` |

## Gotchas that have already cost time

- `uv sync --package fde-training --extra train` REMOVES sibling packages'
  deps from the shared venv. Use an isolated venv (or CI's `train-tests`
  job). Re-fix with `uv sync --all-packages`.
- Root pytest `addopts` already includes `-q`; adding another `-q` gives
  `-qq`, which silently hides the pass/fail summary line.
- Every package has `tests/conftest.py`; pytest imports them all as
  `conftest`. Shared test helpers go in named modules
  (`packages/fde-agents/tests/agent_fakes.py` pattern), and test module
  basenames must be unique across ALL packages' tests directories.
- HNSW probes need the double `::halfvec(1024)` cast or the planner falls
  back to a sequential scan (expression indexes, `db/003`).
- `kg.hybrid_search`'s chunk arm drops chunks with empty `anchor_keys` —
  ingest without anchors and retrieval silently never sees them. The console
  surfaces this as anchor coverage (`/ui/sources`); do not "fix" it in SQL.
- Evidence writes are single-sourced in `fde_mcp.ingest` (size caps, the
  INSERTs, the checksum-dedupe behaviour, the warning text). Two writers run
  them under different roles — `fde_agent` via the MCP tools (db/014),
  `fde_gate_service` via console intake (db/017) — and must stay identical;
  change the contract there, never in one caller.
- Retrieval logic lives in SQL only (`db/008`); `fde_mcp` tools and
  `rollout_env` must stay byte-identical — `test_parity.py` enforces it and
  its divergence whitelist must be updated deliberately, never loosened.
- Docs are tested: `test_docs_sync.py` pins docs/06's reward-weight table to
  `DEFAULT_WEIGHTS` and requires 0.78/0.82 (docs/06) and 0.78 (README) to
  stay present. The CI step name hardcodes the smoke-test count.
- Public docs drift: run the `update-docs` skill; it ends in a mandatory marketing-strategist review.
- MCP tool docstrings are the model-facing prompt — edit them like prompts,
  not comments (`docs/11` §6). New env vars go in the package's `config.py`,
  nowhere else.
- Servers (`fde-mcp`, `fde-embedder`) are env-configured; their `--help` is
  hand-rolled in `__main__.py` / `embedder_worker.py` — keep it working, it
  is the first command a new developer types.
- Model selection is config, not code: `FDE_MODEL_ID` > `FDE_MODEL_PRESET`
  (premium/balanced/budget, per-agent map in `fde_agents/common/config.py`)
  > `DEFAULT_MODEL_ID`. The deploy CLI bakes presets into per-runtime env so
  the proposal audit trail records the authoring model exactly.
- `fde-gate-dev` serves the review console locally through the real
  `lambda_handler` (principal from `FDE_GATE_DEV_PRINCIPAL`; refuses
  anonymous). It shares the handler's module-level event loop — tests that
  touch it must drain the DB pool on THAT loop (see test_devserver.py).

## AWS honesty rule

Nothing here has run against live AWS. Anything touching boto3/Bedrock/
AgentCore ships as "written against verified API shapes, not validated here"
(README "Status and honesty"). Keep that labeling when adding AWS-touching
code; `requires_aws` tests must skip cleanly without credentials.
