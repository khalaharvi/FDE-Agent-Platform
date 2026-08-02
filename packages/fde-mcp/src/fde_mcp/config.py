"""config.py -- one place every `FDE_*` (and the handful of un-prefixed
`KG_*`/`AWS_*`) environment variables are read.

Design
------
Before this module existed, DB pool sizing lived in db.py, embedding
knobs lived in embeddings.py, agent identity lived in server.py, and the
embedder worker's own tuning knobs lived in embedder_worker.py -- four
different files, four different conventions for "what's the default",
and no single place to look to answer "what can I configure and what
happens if I don't". That scatter is exactly what makes a production
incident slower to diagnose: the fix for "the pool is starved" and the
fix for "ef_search is out of the safe band" live in files that have
nothing else to do with each other.

Everything here is grouped into small frozen dataclasses (`AgentSettings`,
`DatabaseSettings`, `EmbeddingSettings`, `EmbedderWorkerSettings`) composed
into one `Settings`, built once from `os.environ` by `Settings.from_env()`
and cached process-wide by `get_settings()`. Frozen because a tool call
reading `get_settings().db.role` mid-request must never observe a value
that changed under it -- these are process configuration, not runtime
state, and treating them as mutable would invite exactly the kind of
"which value was live when this ran" bug that a bitemporal graph platform
should be allergic to.

`get_settings()` is `lru_cache`d rather than a module-level constant so
that tests can call `get_settings.cache_clear()` after monkeypatching
`os.environ` and observe a fresh read -- a plain module-level constant
computed at import time cannot be re-pointed without reloading the module.

Intentionally NOT renamed to `FDE_*`
-------------------------------------
`KG_EF_SEARCH`, `KG_ITERATIVE_SCAN`, and `KG_MAX_SCAN_TUPLES` keep their
existing names (no `FDE_` prefix) because they are deployment-facing
knobs that already exist in runbooks and Terraform/CDK variable files
(see db/008_retrieval.sql's `kg.tune_session`); renaming them here would
silently break every existing deployment that sets them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

_VALID_AGENT_NAMES = ("engagement", "workflow", "development")
_VALID_TRANSPORTS = ("stdio", "http")


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_opt_str(name: str) -> str | None:
    return os.environ.get(name)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_bool(name: str) -> bool:
    """`"1"` is true, everything else (including unset) is false.

    Matches the original `os.environ.get("FDE_DB_IAM_AUTH") == "1"` check
    exactly -- `"true"`/`"yes"` were never accepted, and changing that here
    would silently change which deployments pick IAM auth.
    """
    return os.environ.get(name) == "1"


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Identity carried into every `hitl.proposal` / `trn.trace_step` /
    drift-triage row this process writes, so a merged proposal or a triage
    decision can always be traced back to the exact runtime that produced
    it (see server.py's module docstring, "Agent identity").

    Attributes:
        runtime_arn: `FDE_AGENT_RUNTIME_ARN`. The AgentCore runtime ARN (or
            a `local-dev:` placeholder outside AgentCore) recorded as
            `hitl.proposal.authored_by` / `wf.workflow.authored_by`.
        name: `FDE_AGENT_NAME`. Which of the three agent personas
            (engagement/workflow/development) this process is; validated
            eagerly so a typo fails at startup, not on the first proposal.
        model_id: `FDE_MODEL_ID`. The inference model id in use, recorded
            alongside proposals for audit; optional because not every
            deployment shape threads it through.
        principal: `FDE_PRINCIPAL`. The identity recorded as
            `triaged_by` on drift signals. Defaults to `runtime_arn` when
            unset -- most deployments have exactly one meaningful
            "who did this", and requiring a second env var for it by
            default would be needless ceremony.
        trace_session_id: `FDE_TRACE_SESSION_ID`. When set, every tool
            call appends one row to `trn.trace_step` under this session;
            when unset, tracing is a no-op (see `_base.emit_trace`).
    """

    runtime_arn: str
    name: str
    model_id: str | None
    principal: str
    trace_session_id: str | None

    def __post_init__(self) -> None:
        if self.name not in _VALID_AGENT_NAMES:
            msg = f"FDE_AGENT_NAME={self.name!r} invalid; must be one of {_VALID_AGENT_NAMES!r}"
            raise RuntimeError(msg)

    @classmethod
    def from_env(cls) -> AgentSettings:
        runtime_arn = _env_str("FDE_AGENT_RUNTIME_ARN", "local-dev:fde-mcp-server")
        return cls(
            runtime_arn=runtime_arn,
            name=_env_str("FDE_AGENT_NAME", "engagement"),
            model_id=_env_opt_str("FDE_MODEL_ID"),
            principal=_env_opt_str("FDE_PRINCIPAL") or runtime_arn,
            trace_session_id=_env_opt_str("FDE_TRACE_SESSION_ID"),
        )


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Everything db.py needs to resolve a DSN, size the pool, and tune
    every connection it hands out. See db.py's module docstring for the
    three-way DSN resolution order (`dsn` > `secret_arn` > `iam_auth`) and
    why `SET LOCAL ROLE` + `statement_timeout` are the actual security
    boundary, not the credential used to connect.

    Attributes:
        dsn: `FDE_DB_DSN`. A full libpq connection string. Wins over
            everything else -- the simplest, most explicit option always
            takes precedence over ones that require an extra network call.
        secret_arn: `FDE_DB_SECRET_ARN`. An AWS Secrets Manager secret
            ARN holding `{"host","port","username","password","dbname"}`.
        iam_auth: `FDE_DB_IAM_AUTH=1`. Use RDS/Aurora IAM database
            authentication; needs `host`/`user`/`name`/`aws_region`. The
            token this mints is a ~15-minute password and must be
            re-minted per physical connection -- see db.py's
            `_dsn_from_iam_auth`.
        host: `FDE_DB_HOST`. Required by `iam_auth`; also used as a
            fallback host for `secret_arn` if the secret itself omits one.
        port: `FDE_DB_PORT`, default 5432.
        user: `FDE_DB_USER`. Required by `iam_auth`.
        name: `FDE_DB_NAME`, default "fde". Fallback dbname for
            `secret_arn` and the dbname used by `iam_auth`.
        aws_region: `AWS_REGION` or `AWS_DEFAULT_REGION`. Required by
            `iam_auth`; not `FDE_`-prefixed because it is the standard AWS
            SDK region variable, and inventing a second one would just
            create a place for the two to disagree.
        role: `FDE_DB_ROLE`, default "fde_agent". The role every tool-call
            transaction downgrades to via `SET LOCAL ROLE`. Overridable for
            tests that want to probe grants directly under a different
            role; production code should never need to change it.
        statement_timeout: `FDE_STATEMENT_TIMEOUT`, default "20s". Per-
            tool-call `SET LOCAL statement_timeout`, matching the
            platform's synchronous request/response budget.
        pool_min: `FDE_DB_POOL_MIN`, default 1.
        pool_max: `FDE_DB_POOL_MAX`, default 10.
        pool_timeout: `FDE_DB_POOL_TIMEOUT` seconds, default 30.
        ef_search: `KG_EF_SEARCH`, default 100. HNSW `ef_search`; must be
            in kg.tune_session's safe band 40..200 or the DB itself RAISEs
            at pool-open time -- deliberately not clamped here, see
            db.py's module docstring.
        iterative_scan: `KG_ITERATIVE_SCAN`, default "relaxed_order".
        max_scan_tuples: `KG_MAX_SCAN_TUPLES`, default 20000.
    """

    dsn: str | None
    secret_arn: str | None
    iam_auth: bool
    host: str | None
    port: int
    user: str | None
    name: str
    aws_region: str | None
    role: str
    statement_timeout: str
    pool_min: int
    pool_max: int
    pool_timeout: float
    ef_search: int
    iterative_scan: str
    max_scan_tuples: int

    @classmethod
    def from_env(cls) -> DatabaseSettings:
        return cls(
            dsn=_env_opt_str("FDE_DB_DSN"),
            secret_arn=_env_opt_str("FDE_DB_SECRET_ARN"),
            iam_auth=_env_bool("FDE_DB_IAM_AUTH"),
            host=_env_opt_str("FDE_DB_HOST"),
            port=_env_int("FDE_DB_PORT", 5432),
            user=_env_opt_str("FDE_DB_USER"),
            name=_env_str("FDE_DB_NAME", "fde"),
            aws_region=_env_opt_str("AWS_REGION") or _env_opt_str("AWS_DEFAULT_REGION"),
            role=_env_str("FDE_DB_ROLE", "fde_agent"),
            statement_timeout=_env_str("FDE_STATEMENT_TIMEOUT", "20s"),
            pool_min=_env_int("FDE_DB_POOL_MIN", 1),
            pool_max=_env_int("FDE_DB_POOL_MAX", 10),
            pool_timeout=_env_float("FDE_DB_POOL_TIMEOUT", 30.0),
            ef_search=_env_int("KG_EF_SEARCH", 100),
            iterative_scan=_env_str("KG_ITERATIVE_SCAN", "relaxed_order"),
            max_scan_tuples=_env_int("KG_MAX_SCAN_TUPLES", 20000),
        )


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    """Bedrock embedding client configuration, shared by the MCP server
    (query-side probes) and the embedder worker (index-side writes) --
    see embeddings.py's module docstring for the Titan/Cohere wire-shape
    split this backs.

    Attributes:
        model_id: `FDE_EMBED_MODEL_ID`, default
            "amazon.titan-embed-text-v2:0".
        dimensions: `FDE_EMBED_DIMENSIONS`, default 1024. Must match
            `kg.embedding`'s fixed `vector(1024)` domain
            (003_vectors_hnsw.sql) -- `to_pgvector_literal` enforces this.
        max_retries: `FDE_EMBED_MAX_RETRIES`, default 5. Bedrock throttle
            retry ceiling.
        base_backoff_seconds: `FDE_EMBED_BASE_BACKOFF`, default 0.5.
        max_backoff_seconds: `FDE_EMBED_MAX_BACKOFF`, default 20.0.
        cache_size: `FDE_EMBED_CACHE_SIZE`, default 8192. Max entries in
            the in-process LRU embed cache (a cost optimisation, not a
            correctness dependency -- a cache miss just re-embeds).
        bedrock_region: `FDE_BEDROCK_REGION`, falling back to
            `AWS_REGION`/`AWS_DEFAULT_REGION`, then "us-east-1".
    """

    model_id: str
    dimensions: int
    max_retries: int
    base_backoff_seconds: float
    max_backoff_seconds: float
    cache_size: int
    bedrock_region: str

    @classmethod
    def from_env(cls) -> EmbeddingSettings:
        return cls(
            model_id=_env_str("FDE_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0"),
            dimensions=_env_int("FDE_EMBED_DIMENSIONS", 1024),
            max_retries=_env_int("FDE_EMBED_MAX_RETRIES", 5),
            base_backoff_seconds=_env_float("FDE_EMBED_BASE_BACKOFF", 0.5),
            max_backoff_seconds=_env_float("FDE_EMBED_MAX_BACKOFF", 20.0),
            cache_size=_env_int("FDE_EMBED_CACHE_SIZE", 8192),
            bedrock_region=(
                _env_opt_str("FDE_BEDROCK_REGION")
                or _env_opt_str("AWS_REGION")
                or _env_opt_str("AWS_DEFAULT_REGION")
                or "us-east-1"
            ),
        )


@dataclass(frozen=True, slots=True)
class EmbedderWorkerSettings:
    """Tuning for the standalone `kg.embed_queue` drain worker
    (embedder_worker.py). Separate from `DatabaseSettings.role` because
    the worker deliberately runs as `fde_ingest`, not `fde_agent` -- it
    writes kg.node_embedding/kg.edge_embedding, which the MCP server's
    role cannot (db/010_roles_and_seed_policy.sql).

    Attributes:
        role: `FDE_EMBEDDER_ROLE`, default "fde_ingest".
        batch_size: `FDE_EMBEDDER_BATCH_SIZE`, default 16. Rows claimed
            per `FOR UPDATE SKIP LOCKED` batch.
        poll_interval_seconds: `FDE_EMBEDDER_POLL_SECONDS`, default 5.0.
            Sleep between polls once the queue drains empty.
        max_attempts: `FDE_EMBEDDER_MAX_ATTEMPTS`, default 5. Rows at or
            above this attempt count are left in the queue but no longer
            claimed -- a poison-pill row does not spin the worker forever.
    """

    role: str
    batch_size: int
    poll_interval_seconds: float
    max_attempts: int

    @classmethod
    def from_env(cls) -> EmbedderWorkerSettings:
        return cls(
            role=_env_str("FDE_EMBEDDER_ROLE", "fde_ingest"),
            batch_size=_env_int("FDE_EMBEDDER_BATCH_SIZE", 16),
            poll_interval_seconds=_env_float("FDE_EMBEDDER_POLL_SECONDS", 5.0),
            max_attempts=_env_int("FDE_EMBEDDER_MAX_ATTEMPTS", 5),
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """The whole process configuration, assembled once by `from_env()`
    and handed out by `get_settings()`.

    Attributes:
        transport: `FDE_MCP_TRANSPORT`, default "stdio". Either "stdio"
            (local subprocess JSON-RPC) or "http" (streamable-HTTP on
            0.0.0.0:8080/mcp, the shape AgentCore Runtime requires). Not
            validated here -- see server.main(), which is where an
            invalid value must fail with `SystemExit`, matching this
            process's existing CLI contract rather than a config-loading
            exception.
        agent: See `AgentSettings`.
        db: See `DatabaseSettings`.
        embedding: See `EmbeddingSettings`.
        embedder: See `EmbedderWorkerSettings`.
    """

    transport: str
    agent: AgentSettings
    db: DatabaseSettings
    embedding: EmbeddingSettings
    embedder: EmbedderWorkerSettings

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            transport=_env_str("FDE_MCP_TRANSPORT", "stdio").lower(),
            agent=AgentSettings.from_env(),
            db=DatabaseSettings.from_env(),
            embedding=EmbeddingSettings.from_env(),
            embedder=EmbedderWorkerSettings.from_env(),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide `Settings`, reading `os.environ` on first
    call and caching thereafter.

    Cached rather than read fresh every time because these are process
    configuration, not runtime state -- re-parsing `os.environ` on every
    tool call would let a single mid-flight `os.environ` mutation (a
    plugin, a leaked test fixture) produce different behaviour for two
    calls in the same request, which is a strictly worse failure mode
    than "config is fixed at first use". Tests that need a fresh read
    after monkeypatching the environment should call
    `get_settings.cache_clear()` first.
    """
    return Settings.from_env()


VALID_TRANSPORTS = _VALID_TRANSPORTS
