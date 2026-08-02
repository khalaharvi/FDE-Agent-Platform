"""db.py -- connection layer for the FDE MCP server.

Design contract
----------------
* Every physical connection is tuned for filtered HNSW ANN queries exactly
  once, at checkout, by calling `kg.tune_session(...)` (see
  db/008_retrieval.sql). Skipping this is not a performance regression --
  it is a correctness bug: an untuned session can silently return fewer
  rows than requested from a filtered `ORDER BY embedding <=> q LIMIT k`
  query (pgvector's iterative-scan shortfall). We tune via the pool's
  `configure` callback so it happens for every connection the pool ever
  hands out, not just the first one.

* The MCP server never talks to Postgres as an owner or as `fde_agent`
  directly. It authenticates as an "owner-adjacent" principal (whatever
  `FDE_DB_DSN` / the Secrets Manager secret / IAM auth resolves to) and then
  issues `SET LOCAL ROLE fde_agent` inside every tool-call transaction. That
  role has read-only access to kg.*, may insert/update its own
  hitl.proposal(_item) rows, may insert its own trn.trace_session/step rows,
  and a handful of narrowly-scoped write grants added in
  db/011_mcp_agent_supplemental_grants.sql for drift triage and draft
  workflow authoring. See that file for exactly what was added and why --
  the "owner-adjacent" credential is only ever a ceiling; `SET LOCAL ROLE`
  is the actual security boundary enforced by Postgres for the duration of
  the transaction.

* `SET LOCAL ROLE` and `SET LOCAL statement_timeout` are transaction-scoped
  in Postgres, so both must be issued *inside* the same transaction as the
  tool's real work, and that transaction must be short-lived. The
  `tool_transaction()` context manager below is the only sanctioned way to
  get a connection in this codebase; nothing should call `pool.connection()`
  directly.

DSN resolution order (first one set wins) -- see `config.DatabaseSettings`
for the exact env vars involved:
  1. dsn           -- a full libpq connection string
  2. secret_arn    -- AWS Secrets Manager secret holding
                       {"host","port","username","password","dbname"};
                       fetched once and cached for the process lifetime.
  3. iam_auth      -- IAM database authentication via
                       `rds.generate_db_auth_token`. The token is a
                       ~15-minute password, so it is re-minted on every new
                       physical connection the pool opens (see
                       `_dsn_from_iam_auth`, called from `_resolve_dsn`,
                       which the pool invokes whenever it needs to open a
                       fresh connection).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from fde_mcp.config import DatabaseSettings, get_settings
from fde_mcp.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger(__name__)

# A connection tuned and rowed exactly the way every tool call in this
# server needs. Spelled out once so the pool, the DSN resolvers, and
# tool_transaction() all agree on what "a connection" means here.
Connection = psycopg.AsyncConnection[dict[str, Any]]


class ConfigError(RuntimeError):
    """Raised when the environment does not describe a usable DB connection."""


async def _dsn_from_secrets_manager(settings: DatabaseSettings, secret_arn: str) -> str:
    """Resolve a DSN from an AWS Secrets Manager secret.

    Expected secret JSON shape (the standard RDS/Aurora-managed rotation
    shape): {"host","port","username","password","dbname"}. `dbname` falls
    back to `settings.name` if the secret does not carry it (e.g. a
    cluster-level credential rotated independently of the database name).
    """
    import boto3  # noqa: PLC0415 -- keep boto3 optional for stdio-only, DSN-only deployments

    def _fetch() -> str:
        client = boto3.client("secretsmanager", region_name=settings.aws_region)
        resp = client.get_secret_value(SecretId=secret_arn)
        return str(resp["SecretString"])

    raw = await asyncio.to_thread(_fetch)
    secret: dict[str, Any] = json.loads(raw)
    host = secret.get("host") or settings.host
    if not host:
        msg = f"secret {secret_arn} has no 'host' and FDE_DB_HOST is not set"
        raise ConfigError(msg)
    port = secret.get("port", 5432)
    user = secret["username"]
    password = secret["password"]
    dbname = secret.get("dbname") or settings.name
    return (
        f"host={host} port={port} dbname={dbname} user={user} password={password} sslmode=require"
    )


async def _dsn_from_iam_auth(settings: DatabaseSettings) -> str:
    """Resolve a DSN using an RDS/Aurora IAM auth token as the password.

    The token is valid for ~15 minutes, so this is only safe to call once
    per physical connection (the pool calls `_resolve_dsn` again whenever it
    needs to open a fresh connection -- see `get_pool`). It is NOT safe to
    cache the resulting DSN string across reconnects.
    """
    import boto3  # noqa: PLC0415 -- same rationale as above

    if not settings.host:
        raise ConfigError("FDE_DB_IAM_AUTH=1 requires FDE_DB_HOST")
    if not settings.user:
        raise ConfigError("FDE_DB_IAM_AUTH=1 requires FDE_DB_USER")
    if not settings.aws_region:
        raise ConfigError("FDE_DB_IAM_AUTH=1 requires AWS_REGION")

    host, port, user, region = settings.host, settings.port, settings.user, settings.aws_region

    def _mint() -> str:
        client = boto3.client("rds", region_name=region)
        return str(
            client.generate_db_auth_token(
                DBHostname=host, Port=port, DBUsername=user, Region=region
            )
        )

    token = await asyncio.to_thread(_mint)
    return (
        f"host={host} port={port} dbname={settings.name} user={user} "
        f"password={token} sslmode=require"
    )


async def _resolve_dsn() -> str:
    settings = get_settings().db
    if settings.dsn:
        return settings.dsn
    if settings.secret_arn:
        return await _dsn_from_secrets_manager(settings, settings.secret_arn)
    if settings.iam_auth:
        return await _dsn_from_iam_auth(settings)
    raise ConfigError(
        "no database connection configured: set one of FDE_DB_DSN, "
        "FDE_DB_SECRET_ARN, or FDE_DB_IAM_AUTH=1 (with FDE_DB_HOST/"
        "FDE_DB_USER/FDE_DB_NAME/AWS_REGION)"
    )


async def _configure_connection(conn: Connection) -> None:
    """Pool `configure` callback: runs once per NEW physical connection.

    This is where `kg.tune_session` is called -- see the module docstring.
    Also fixes the row factory so every cursor on this connection returns
    plain JSON-serialisable dicts (`psycopg.rows.dict_row`), per the MCP
    server's contract that every tool result is JSON-serialisable.
    """
    conn.row_factory = dict_row
    settings = get_settings().db
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT kg.tune_session(%s, %s, %s)",
            (settings.ef_search, settings.iterative_scan, settings.max_scan_tuples),
        )
    await conn.commit()


_pool: AsyncConnectionPool[Connection] | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> AsyncConnectionPool[Connection]:
    """Return the process-wide connection pool, opening it on first use."""
    global _pool  # noqa: PLW0603 -- process-wide pool is inherently global
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            settings = get_settings().db
            dsn = await _resolve_dsn()
            pool: AsyncConnectionPool[Connection] = AsyncConnectionPool[Connection](
                conninfo=dsn,
                min_size=settings.pool_min,
                max_size=settings.pool_max,
                configure=_configure_connection,
                open=False,
                timeout=settings.pool_timeout,
            )
            await pool.open(wait=True, timeout=30)
            log.info(
                "db_pool_opened",
                min_size=pool.min_size,
                max_size=pool.max_size,
                ef_search=settings.ef_search,
                iterative_scan=settings.iterative_scan,
            )
            _pool = pool
    return _pool


async def close_pool() -> None:
    """Close the process-wide pool. Call on server shutdown."""
    global _pool  # noqa: PLW0603 -- process-wide pool is inherently global
    async with _pool_lock:
        if _pool is not None:
            await _pool.close()
            _pool = None


async def set_role(conn: Connection, role: str | None = None) -> None:
    """Issue `SET LOCAL ROLE <role>` on `conn`.

    Must be called inside an open transaction -- `SET LOCAL` is a no-op
    outside one (it would silently apply for the rest of the session
    instead of just the current transaction, which is exactly the
    escalation-persists-too-long bug this function exists to prevent).
    `role` is interpolated as an identifier via `psycopg.sql`, never via
    string formatting, so this is not adjustable to a caller-supplied
    string it hasn't been vetted against.
    """
    resolved_role = role if role is not None else get_settings().db.role
    async with conn.cursor() as cur:
        await cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(resolved_role)))


@contextlib.asynccontextmanager
async def tool_transaction(
    role: str | None = None,
    statement_timeout: str | None = None,
) -> AsyncIterator[Connection]:
    """The only sanctioned way to get a DB connection for a tool call.

    Opens one transaction, sets a per-call statement_timeout, downgrades to
    `role` via SET LOCAL ROLE, yields the connection, and commits on clean
    exit / rolls back on exception. Because both SETs are LOCAL, they
    evaporate with the transaction regardless of outcome -- a connection
    handed back to the pool is always back at the owner-adjacent role with
    the default statement_timeout.

    `role`/`statement_timeout` default to `get_settings().db` when omitted;
    callers (mainly the embedder worker) that need a different role or a
    longer timeout pass them explicitly.
    """
    settings = get_settings().db
    resolved_role = role if role is not None else settings.role
    resolved_timeout = (
        statement_timeout if statement_timeout is not None else settings.statement_timeout
    )
    pool = await get_pool()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(resolved_timeout))
        )
        await set_role(conn, resolved_role)
        yield conn
