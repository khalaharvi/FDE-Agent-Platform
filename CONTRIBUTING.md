# Contributing

Thanks for looking under the hood. This repo optimizes for one thing:
**every claim in the docs is either tested or labeled unverified.** PRs
that keep that property are easy to merge; PRs that break it are not.

## Setup (the 5-minute path)

```bash
git clone <this repo> && cd fde-platform
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv manages Python itself
uv sync --all-packages --frozen
docker compose up -d db                            # pgvector 0.8, port 55432
export PGHOST=localhost PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres
./db/rebuild.sh fde                                # 18 migrations + 25 smoke tests
export FDE_DB_DSN="postgresql://postgres:postgres@localhost:55432/fde"
uv run pytest packages                             # 914 tests
```

Already run Postgres locally with pgvector ≥ 0.8? Skip compose:
`./db/rebuild.sh fde && export FDE_DB_DSN=postgresql:///fde` replaces
everything from `docker compose` down to the DSN export. The README adds
`--with-demo` to seed a reviewable engagement; the gates do not need it either
way.

## The gates your PR must pass

Exactly what CI runs — run them before pushing:

```bash
uv run ruff check packages && uv run ruff format --check packages
uv run mypy                       # strict: fde-mcp, fde-agents, fde-gate, fde-sor
./db/rebuild.sh fde_ci            # migrations apply in order, smoke tests pass
FDE_DB_DSN=... uv run pytest packages
uv lock --check                   # lockfile must match pyproject changes
```

`pre-commit install` gives you the fast subset on every commit.

## Rules that are enforced, not suggested

- **The invariant is sacred.** Agents propose, humans dispose. CI asserts
  twenty-nine privilege denials; your change must keep all twenty-nine failing. New
  SECURITY DEFINER functions need `REVOKE ALL ... FROM PUBLIC` + explicit
  grants with a comment saying why (see db/011 for the style).
- **Retrieval logic lives in SQL only** (`db/008`). The MCP tools and the
  RL rollout env must stay byte-identical — `test_parity.py` enforces it.
  Update the divergence whitelist deliberately or your PR fails.
- **Docs are tested.** `test_docs_sync.py` pins docs/06's reward-weight
  table to `DEFAULT_WEIGHTS` and the kappa thresholds to the code. If you
  change one side, change both.
- **Conventions are binding**: `docs/11-python-conventions.md`. Highlights:
  `from __future__ import annotations` everywhere, env vars declared once
  in each package's `config.py`, structlog with stable event keys (never
  f-strings into the message), no `print()` outside CLIs/deploy.
- **New tables/functions in a migration get no grants automatically** —
  grant explicitly, and add a smoke test (the CI step name hardcodes the
  smoke-test count; bump it).
- **AWS honesty**: nothing here is validated against live AWS. Keep the
  "written against verified API shapes, not validated here" labeling for
  AWS-touching code, and make `requires_aws` tests skip cleanly.

## Gotchas (read before your first test run)

- `uv sync --package fde-training --extra train` strips sibling packages
  from the shared venv — use an isolated venv for train-extra work (CI
  runs it in its own job).
- pytest `addopts` already includes `-q`; a second `-q` hides summaries.
- Test module basenames must be unique across ALL packages (pytest imports
  every `tests/conftest.py` as `conftest`); shared helpers go in named
  modules like `packages/fde-agents/tests/agent_fakes.py`.

## PRs

Small and focused beats large and heroic. Include: what changed, why,
test evidence (paste the pytest tail), and a CHANGELOG entry under
`[Unreleased]`. If you found a bug, add it to the ledger in
`docs/99-sources.md` §8 — the ledger is a feature.

Cutting a release from `[Unreleased]` is a separate step — see `RELEASING.md`.
