"""tools/_base.py -- the machinery every tool module shares.

Nothing in here reimplements graph, retrieval, or gating logic (that stays
in the database, see db/008_retrieval.sql's module docstring). What lives
here is the boundary work that would otherwise be copy-pasted into every
tool: classifying a psycopg error into "the model can fix this" versus
"this is our problem", making a fetched row JSON-safe, and best-effort
training-trace emission. Centralising it means the self-correctable/opaque
classification (see `pg_error_boundary`) is applied identically everywhere
instead of drifting tool-by-tool as new ones are added.

Trace-emission ordering (`emit_trace`)
---------------------------------------
`jsonify(result)` MUST run before the value is wrapped in `Jsonb(...)` for
the `trn.trace_step` insert. psycopg hands back native `uuid.UUID` /
`datetime` objects for uuid/timestamptz columns, and `Jsonb` serialises its
payload with plain `json.dumps`, which raises `TypeError` on those types.
Get the order backwards and the trace insert silently fails for exactly the
tools whose results contain ids and timestamps -- i.e. almost all of them --
and the RL/SFT training substrate quietly ends up empty while every tool
call still appears to succeed. `emit_trace` enforces the order in one place
so no future tool can reintroduce the bug by wrapping its own insert.
"""

from __future__ import annotations

import functools
import json
import time
import uuid as uuid_mod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import psycopg
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from fde_mcp.config import get_settings
from fde_mcp.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared graph vocabulary. Both graph.py (search/traverse filters) and
# proposals.py (proposal item node_type/edge_type) validate against these,
# so they live here rather than being duplicated.
# ---------------------------------------------------------------------------
NODE_TYPES: tuple[str, ...] = (
    "org_unit",
    "role",
    "system",
    "system_object",
    "capability",
    "process",
    "activity",
    "artifact",
    "decision",
    "control",
    "metric",
    "pain_point",
    "opportunity",
    "tool_binding",
    "evidence_doc",
)
EDGE_TYPES: tuple[str, ...] = (
    "belongs_to",
    "performs",
    "precedes",
    "hands_off_to",
    "produces",
    "consumes",
    "recorded_in",
    "depends_on",
    "gated_by",
    "measured_by",
    "blocks",
    "addresses",
    "automatable_by",
    "evidenced_by",
    "supersedes",
)
# `Literal[NODE_TYPES]` (not `Literal[*NODE_TYPES]`) is deliberate: typing's
# `__class_getitem__` treats a single tuple argument as the parameter list,
# so this is equivalent to spelling out `Literal["org_unit", "role", ...]`
# without repeating the vocabulary. mypy cannot see through the indirection
# (hence the ignore), but pydantic evaluates it at runtime and validates
# exactly as if it had been spelled out.
NodeType = Literal[NODE_TYPES]  # type: ignore[valid-type]
EdgeType = Literal[EDGE_TYPES]  # type: ignore[valid-type]

TRACE_RESULT_MAX_BYTES = 8192


def _pg_message(exc: psycopg.Error) -> str:
    diag = exc.diag  # always a Diagnostic; its fields are None if unavailable
    if diag.message_primary:
        msg = diag.message_primary
        if diag.message_detail:
            msg = f"{msg} DETAIL: {diag.message_detail}"
        return msg
    return str(exc).strip()


_SELF_CORRECTABLE_CLASSES: tuple[type[psycopg.Error], ...] = (
    pg_errors.RaiseException,  # plain plpgsql RAISE EXCEPTION (SQLSTATE P0001)
    pg_errors.CheckViolation,
    pg_errors.NotNullViolation,
    pg_errors.ForeignKeyViolation,
    pg_errors.UniqueViolation,
    pg_errors.ExclusionViolation,
    pg_errors.InvalidTextRepresentation,  # e.g. malformed uuid / enum literal
    pg_errors.InvalidParameterValue,
    pg_errors.DatatypeMismatch,
)


def _is_self_correctable(exc: psycopg.Error) -> bool:
    return isinstance(exc, _SELF_CORRECTABLE_CLASSES)


def _hint_for(exc: psycopg.Error) -> str:
    if isinstance(exc, pg_errors.InsufficientPrivilege):
        msg = _pg_message(exc)
        if "materialized view" in msg.lower():
            return (
                "sor.run_all_detectors() calls REFRESH MATERIALIZED VIEW "
                "CONCURRENTLY, which requires the calling role to OWN "
                "sor.observed_transition (Postgres 16 has no grantable "
                "REFRESH privilege). This is a schema/ownership gap, not a "
                "bad argument -- either run the refresh out-of-band as the "
                "matview owner, or grant that ownership/role membership to "
                "fde_agent."
            )
        return (
            "a DB grant is missing for this operation under the current "
            "role. Check db/010_roles_and_seed_policy.sql and "
            "db/011_mcp_agent_supplemental_grants.sql."
        )
    if isinstance(
        exc, pg_errors.OperationalError | pg_errors.AdminShutdown | pg_errors.CrashShutdown
    ):
        return "looks transient (connection/availability); safe to retry once."
    if isinstance(exc, pg_errors.QueryCanceled):
        return (
            "the query exceeded the 20s per-tool statement_timeout; narrow "
            "k / max_hops / max_nodes and retry."
        )
    if isinstance(exc, pg_errors.UndefinedFunction | pg_errors.UndefinedTable):
        return "the DB schema does not have this function/table -- check for a migration mismatch."
    return "unexpected database error; check server logs for the full exception."


def jsonify(value: Any) -> Any:
    """Recursively coerce psycopg's native Python types (`uuid.UUID`,
    `datetime`) into JSON-safe values.

    `dict_row` hands back UUID/datetime objects for uuid/timestamptz
    columns, and every tool result must be JSON-serialisable per this
    server's contract with its MCP clients. This runs once at the tool
    boundary (`pg_error_boundary`) and again, deliberately, before trace
    emission (`emit_trace`) -- see this module's docstring for why the
    second call is not redundant.
    """
    if isinstance(value, dict):
        return {k: jsonify(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonify(v) for v in value]
    if isinstance(value, uuid_mod.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def pg_error_boundary[**P](
    func: Callable[P, Awaitable[dict[str, Any]]],
) -> Callable[P, Awaitable[dict[str, Any]]]:
    """Decorator: classify psycopg errors per the error-handling contract
    described in server.py's module docstring.

    A Postgres error the model can plausibly fix by changing its own
    arguments -- a plpgsql `RAISE EXCEPTION` or a constraint violation --
    is re-raised as a plain `RuntimeError` carrying the message verbatim,
    so the MCP client sees it and the model can self-correct. Anything
    else (connection drop, permission/config problem, an unexpected
    server error) is caught and returned as `{"error": ..., "hint": ...}`
    so one flaky query cannot tear down the whole tool-call loop.
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        try:
            result = await func(*args, **kwargs)
            return cast("dict[str, Any]", jsonify(result))
        except psycopg.Error as exc:
            message = _pg_message(exc)
            if _is_self_correctable(exc):
                log.info("tool_rejected_input", tool=func.__name__, message=message)
                raise RuntimeError(message) from None
            log.exception("tool_db_error", tool=func.__name__)
            return {"error": message, "hint": _hint_for(exc)}

    return wrapper


def _truncate_for_trace(obj: Any, max_bytes: int = TRACE_RESULT_MAX_BYTES) -> Any:
    raw = json.dumps(obj, default=str)
    encoded = raw.encode("utf-8")
    if len(encoded) <= max_bytes:
        return obj
    preview = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return {"_truncated": True, "_original_bytes": len(encoded), "preview": preview}


async def emit_trace(
    conn: psycopg.AsyncConnection[dict[str, Any]],
    tool_name: str,
    result: Any,
    *,
    retrieval: dict[str, Any] | None = None,
    latency_ms: int | None = None,
) -> None:
    """Best-effort `trn.trace_step` insert.

    Never lets a tracing failure (typically: `FDE_TRACE_SESSION_ID` points
    at a session row that does not exist yet) affect the tool's actual
    result -- runs in a SAVEPOINT (a nested `conn.transaction()`), so only
    the trace insert rolls back on failure, not the tool's own writes.
    """
    trace_session_id = get_settings().agent.trace_session_id
    if not trace_session_id:
        return
    try:
        async with conn.transaction(), conn.cursor() as cur:  # nested -> SAVEPOINT
            await cur.execute(
                """
                INSERT INTO trn.trace_step
                    (session_id, turn, role, tool_name, tool_result,
                     trainable, retrieval, latency_ms)
                SELECT %(session_id)s::uuid, COALESCE(MAX(turn), 0) + 1, 'tool',
                       %(tool_name)s, %(tool_result)s, false, %(retrieval)s,
                       %(latency_ms)s
                  FROM trn.trace_step WHERE session_id = %(session_id)s::uuid
                """,
                {
                    "session_id": trace_session_id,
                    "tool_name": tool_name,
                    # jsonify() BEFORE Jsonb() -- see module docstring.
                    "tool_result": Jsonb(_truncate_for_trace(jsonify(result))),
                    "retrieval": Jsonb(retrieval) if retrieval is not None else None,
                    "latency_ms": latency_ms,
                },
            )
    except Exception:
        log.warning(
            "trace_emit_failed",
            tool=tool_name,
            session_id=trace_session_id,
            exc_info=True,
        )


def now_ms() -> float:
    return time.monotonic() * 1000.0


def parse_timestamp(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


async def fetchall(cur: psycopg.AsyncCursor[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(await cur.fetchall())


async def fetchone(cur: psycopg.AsyncCursor[dict[str, Any]]) -> dict[str, Any] | None:
    return await cur.fetchone()
