# Python Conventions

The contract for the Python in this repo. Short, because the tooling enforces
most of it — this file exists to explain the choices the config cannot.

---

## 1. Toolchain

**`uv` is the only prerequisite.** It manages the Python interpreter, the
virtualenv, dependency resolution, and the lockfile. There is no `pip install`
step anywhere, including in the Dockerfiles.

```bash
uv sync --all-packages --frozen   # exactly what uv.lock pins, or fail
uv run pytest packages
uv run ruff check packages
uv run mypy packages/fde-mcp/src packages/fde-agents/src
```

`--frozen` everywhere — local, CI, and both Dockerfiles. A container that
quietly resolves a different dependency set than CI tested is the entire
problem lockfiles exist to solve, and the failure is invisible until
production. `uv lock --check` is a CI gate, and `uv-lock` is a pre-commit hook
so a `pyproject.toml` change cannot land without a regenerated lockfile.

---

## 2. Why five packages

The split is along **deployment boundaries**, not along what feels related.

| Package | Runs where | Why it is separate |
|---|---|---|
| `fde-mcp` | its own service (ECS/Fargate) or in-runtime | holds the DB credentials; smallest and most security-sensitive dependency set |
| `fde-agents` | three AgentCore runtimes | depends on `fde-mcp` for the tracing path, not for tools (those arrive over MCP) |
| `fde-training` | offline, SageMaker | heavy ML stack that must **never** reach an agent image |
| `fde-gate` | one Lambda behind API Gateway | the human surface; runs as `fde_gate_service`/`fde_prodops`, the only roles that can merge and run — those credentials never share an artifact with agent code |
| `fde-sor` | Lambdas (EventBridge/SQS) or EKS CronJobs | SoR adapters run as `fde_ingest` with per-customer source credentials; separate blast radius and lifecycle from everything else |

That last row is the one that earns the structure. `torch`, `trl`, `peft`,
`transformers` live in `fde-training`'s optional `train` extra. `fde-agents`
does not depend on `fde-training`, so `uv sync --package fde-agents`
**structurally cannot** pull them — verified in CI, not left to discipline.
A single flat package would have made "don't ship torch to the agent image" a
convention someone eventually forgets.

`src/` layout throughout: tests import the installed package, not the source
tree next to them, so a missing `__init__.py` or a bad `[tool.hatch.build]`
entry fails in CI rather than in the first deployed container.

---

## 3. Style, and what enforces it

| Rule | Enforced by |
|---|---|
| `from __future__ import annotations` in every module | ruff `isort.required-imports` |
| PEP 604 unions — `X \| None`, `dict[str, Any]` | ruff `UP` |
| No blocking calls inside `async def` | ruff `ASYNC` |
| No naive datetimes | ruff `DTZ` — everything here is `timestamptz` |
| No `print()` in library code | ruff `T20` (per-file-ignored for CLIs, where printing *is* the interface) |
| `pathlib` over `os.path` | ruff `PTH` |
| Security lints | ruff `S` (bandit) |
| Full type annotations | `mypy --strict` |

**`mypy --strict` covers `fde-mcp` and `fde-agents` only.** `fde-training` is
deliberately excluded and the root `pyproject.toml` says so inline. It is
research code whose shape changes with every experiment; holding it to strict
produces a drift of `# type: ignore` comments that carry no information and
train people to add them reflexively. Being explicit about the boundary is
more honest than a uniform setting nobody actually holds to. Public functions
there are still annotated — the rewards module especially, since it is the
most-reviewed code in the package.

Third-party packages without stubs are listed **by name** in the mypy
overrides rather than blanket `ignore_missing_imports`. A new untyped
dependency stays visible instead of being silently absorbed.

---

## 4. Configuration

**One typed settings object per package. No `os.getenv` outside it.**

```python
from fde_mcp.config import get_settings

settings = get_settings()          # lru_cached, frozen dataclasses
dsn = settings.database.dsn
```

Every `FDE_*` variable has exactly one declaration site with a type, a
default, and a docstring saying what it does and what happens if it is wrong.
Before this, env access was spread across four files and the only way to
enumerate the configuration surface was to grep. That is the difference
between an operator being able to configure the system and having to read it.

`fde_training.config` reuses `fde_mcp.config.DatabaseSettings` rather than
redeclaring DB variables — one definition, one place to change a default.

---

## 5. Logging

**`fde_mcp.logging`, structured, JSON by default.**

```python
from fde_mcp.logging import get_logger, bind_session

log = get_logger(__name__)
bind_session(session_id, engagement_id=eng, agent="engagement")
log.info("tool_call", tool="kg_search", k=20, latency_ms=38)
```

Never f-strings into the message. The event name is a stable key; the
variables are fields. That is what makes a CloudWatch Logs Insights query
possible at all:

```
fields @timestamp, event, tool, latency_ms
| filter session_id = '<id>' | sort @timestamp asc
```

Two design points worth knowing:

**`session_id` is bound into contextvars, not passed through signatures.**
AgentCore's `runtimeSessionId`, the OTEL session id, and
`trn.trace_session.session_id` are deliberately the same value, so one field
joins agent logs, MCP tool logs, and the training trace table. MCP tool
handlers are `async def` dispatched by FastMCP with no seam to thread a bound
logger through; contextvars propagate into `asyncio.gather` children.

**stdlib records route through the same processor chain**
(`structlog.stdlib.ProcessorFormatter`). Without it, a deployed process emits
two interleaved formats — JSON from our code, printf from boto3, psycopg, and
the AgentCore SDK — and the Insights queries see half the story.

`FDE_LOG_CONSOLE=1` gives human-readable output locally. Never set it in a
deployed runtime; CloudWatch cannot query it.

---

## 6. Module boundaries

**`fde_mcp` is a thin typed wrapper over `db/008_retrieval.sql`.** No
retrieval logic in Python. The reason is in `docs/06-training.md`: RL rollouts
run against the same SQL functions production serves, and any logic that lives
in the Python layer is logic the rollout environment can diverge on. That
divergence is silent — the training curves look fine while you optimise a
policy for a world it will never see.

**Tools are split by concern**, not held in one file:
`tools/{graph,proposals,drift,workflow}.py`, with `tools/_base.py` holding the
error boundary, JSON coercion, and trace emission. `server.py` builds the
FastMCP instance and calls `register_all()`. The 23 tools and their docstrings
are unchanged from before the split — **the docstrings are the model-facing
tool descriptions**, so they are a tuned deliverable, not commentary. Edit them
the way you would edit a prompt.

**`fde_agents/common/runtime.py` holds everything the three agents share** —
app construction, ping handler, async-task dispatch, streaming loop, error
envelope, session binding. Each `agent.py` contributes only its prompt, its
tool allowlist, and its task handlers. Before this the three files were 788
lines of largely duplicated scaffolding; they are now 498, and a reviewer can
see that the three agents are three configurations of one runtime rather than
three copy-pasted files. **If you find yourself editing the same thing in all
three `agent.py` files, it belongs in `runtime.py`.**

---

## 7. Errors

Two contracts, and the distinction is load-bearing:

- **Infrastructure failures** → structured `{"error": ..., "hint": ...}`. The
  model cannot fix a connection timeout; it needs to know to stop trying.
- **Validation failures** → the Postgres `RAISE` message, **verbatim**. When
  `hitl.submit_proposal` says *"proposal 42 has 3 item(s) with no evidence
  source"*, that is precisely the information the model needs to correct
  itself. Wrapping or paraphrasing it destroys the self-correction loop.

Never `except Exception: pass`. Tracing and telemetry writes are the one place
a swallowed exception is correct — a failed trace insert must not break a tool
call — and those run inside a `SAVEPOINT` and log at `info` with a reason.

---

## 8. Containers

Multi-stage, `ghcr.io/astral-sh/uv` builder → `python:3.12-slim` runtime.
Dependency resolution is split from source copy via `--no-install-project`, so
a code change does not invalidate the dependency layer.

**ARM64 is mandatory** for the AgentCore container path, and getting it wrong
is a silent failure: the image pushes fine and runs fine on an x86 laptop under
emulation, then fails in the runtime. Both Dockerfiles pin
`--platform=linux/arm64` in the `FROM` rather than relying on the build
invocation.

Non-root (`uid 1001`). AgentCore gives each session an isolated microVM, so
this is defence in depth rather than the primary boundary — but a process that
does not need root should not have it.

**One Dockerfile for all three agents**, selected by `ARG AGENT`. Three
near-identical Dockerfiles is exactly the duplication `runtime.py` removed from
the Python; it should not survive in the build. There is a build-time
`python -c "importlib.import_module(...)"` check so a bad `AGENT` value fails
the build instead of the first invocation.

---

## 9. Tests

914 tests. `pytest` with `asyncio_mode = "auto"`, `--strict-markers`,
`--strict-config`, and `filterwarnings = ["error"]` — a new
`DeprecationWarning` fails the build rather than scrolling past.

Two markers: `requires_db` (skips cleanly without `FDE_DB_DSN`) and
`requires_aws` (always skipped in CI). CI runs against a real
`pgvector/pgvector:0.8.0-pg16` service so the DB-marked tests actually execute
rather than silently skipping — a skipped test suite that reports green is
worse than no suite.

**The invariant tests are not pytest.** Twenty-nine SQL statements in the CI
`database` job that must all be *denied*. The first eight are the four-layer
invariant itself and are not to be edited:

```
fde_agent      → INSERT kg.node                      must fail
fde_agent      → hitl.merge_proposal                 must fail
fde_agent      → UPDATE wf.workflow SET status=...   must fail
fde_agent      → UPDATE trn.trace_session.outcome    must fail
fde_rl_rollout → INSERT hitl.proposal                must fail
fde_rl_rollout → UPDATE trn.trace_session.outcome    must fail
fde_agent      → wf.publish_workflow                 must fail
fde_prodops    → UPDATE wf.workflow SET status=...   must fail
```

The other twenty-one guard the surfaces added since. Twelve are the reviewer
roster (`db/016`): nothing but the console may write it, and even the console
may not delete a reviewer or rewrite a principal. Five are evidence intake
(`db/017`) — four pin the console's `INSERT` on
`kg.source`/`kg.chunk`/`kg.embed_queue` so it never widens into `UPDATE`,
`DELETE`, or the graph tables, and the fifth denies that same `INSERT` to a
different role entirely, `fde_prodops`, which reads the graph and runs
workflows, because widening it *because it is also the console* would land an
evidence write on the role picked for being narrow.

The last four are the agent-launch record (`db/018`), the same shape one table
over. `wf.agent_launch` says who asked for which agent task, so an agent that
could `INSERT` there could attribute its own work to a human who never asked
for it, and one that could `UPDATE` could mark its own launch succeeded — the
invariant defeated in the audit trail rather than in the graph. The console
writes the row and may stamp only how it ended; rewriting `principal` is
denied for the same reason `db/016` denies it on `hitl.reviewer`, and nothing
holds `DELETE`.

If any succeeds, an agent can write the graph, label its own training data, or
appoint its own reviewer, and the platform's central guarantee is gone.
Asserted every run rather than trusted. Adding a grant means adding a denial
that proves its boundary; all twenty-nine must keep failing.

---

## 10. Adding things

**A new MCP tool** → the right `tools/*.py` module; typed args with
`Annotated[..., Field(...)]`; a docstring written as a prompt; `@_pg_error_boundary`;
a test in the matching `tests/test_*_tools.py`.

**A new agent task** → a handler in that agent's `agent.py` and an entry in its
config. If it needs new runtime scaffolding, that goes in `common/runtime.py`,
not a fourth copy.

**A new reward term** → the right `rewards/*.py` module, a weight in
`DEFAULT_WEIGHTS` (they sum to 1.0 — rebalance, do not append), a module
docstring naming the failure mode it targets and the evidence for it, and unit
tests with hand-constructed episodes.

**A new environment variable** → the package's `config.py`. Nowhere else.

**A new dependency** → `uv add --package <member> <dep>`, commit `uv.lock`.
Check it does not land in `fde-agents`' tree unless an agent genuinely needs it.
