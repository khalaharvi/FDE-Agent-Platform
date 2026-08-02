"""common.py -- shared utilities for the training pipeline.

Design decision: one shared module rather than duplicating DB-connection and
canonicalisation logic in every script (`export_sft.py`, `rollout_env.py`,
`rival_grader.py`, `generate_traces.py`). The single most expensive failure
mode called out for this whole pipeline is a silent loss-masking bug caused
by a converter re-deriving something it should have read verbatim (see
`db/009_training.sql`'s trace_step docstring); consolidating the
canonicalisation/volatile-stripping logic in one place means there is only
one implementation to audit, not four that can silently drift apart.

Everything here is stdlib + psycopg (+ `fde_mcp` for config/logging only).
No torch/trl/peft dependency, so every other module in this package can
`import fde_training.common` without pulling in the heavy training stack --
important for `export_sft.py`, which is a pure ETL script that should run
on a laptop with no GPU and no TRL install.

DB connection model
--------------------
Mirrors `fde_mcp.db`'s security contract (`SET LOCAL ROLE` inside every
transaction) but is deliberately a separate, synchronous, psycopg3
implementation: the training pipeline is a batch of independent scripts run
from a shell or an orchestrator (Airflow/Step Functions/a cron), not a
long-lived async server process, so there is no pool lifecycle to manage
and no reason to pull in `psycopg_pool` or `asyncio` here. `rollout_env.py`
is the one module in this package that DOES need a connection pool (many
concurrent RL rollouts) and it builds its own, sync, `psycopg_pool
.ConnectionPool`, on top of `resolve_dsn()` below -- see that module's own
docstring for why it cannot reuse `fde_mcp.db`'s async pool.

DSN resolution deliberately does NOT reimplement `fde_mcp.db`'s
secret_arn/iam_auth machinery -- see `fde_training.config`'s module
docstring for why that is a considered omission, not an oversight.
"""

from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import hashlib
import json
import re
import uuid as _uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from fde_mcp.logging import get_logger
from fde_training.config import get_settings

if TYPE_CHECKING:
    from types import TracebackType

log = get_logger(__name__)


# ===========================================================================
# DSN resolution -- see fde_training.config's module docstring for why this
# is a strict subset of fde_mcp.db's resolution order. Training jobs that
# must run against IAM-authenticated RDS should export FDE_DB_DSN with a
# freshly minted token (e.g. from `aws rds generate-db-auth-token` in a
# wrapper script) rather than this module re-implementing token minting.
# ===========================================================================
def resolve_dsn() -> str:
    """`FDE_DB_DSN` if set, else a local peer-auth fallback against
    `FDE_DB_NAME` (default "fde") -- exactly the environment this pipeline
    was developed and tested against (see the package README).
    """
    db = get_settings().db
    if db.dsn:
        return db.dsn
    return f"dbname={db.name}"


class TrainingDBConnection:
    """A psycopg3 connection wrapper that issues `SET LOCAL ROLE` inside every
    transaction, mirroring `fde_mcp.db.tool_transaction`'s security contract.
    Use via `connect()` below, not directly.
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    @property
    def conn(self) -> psycopg.Connection[Any]:
        return self._conn

    def cursor(self) -> psycopg.Cursor[dict[str, Any]]:
        return self._conn.cursor(row_factory=dict_row)


class _ConnectCtx:
    def __init__(self, dsn: str, role: str, *, autocommit: bool) -> None:
        self.dsn = dsn
        self.role = role
        self.autocommit = autocommit
        self._conn: psycopg.Connection[Any] | None = None

    def __enter__(self) -> TrainingDBConnection:
        self._conn = psycopg.connect(self.dsn, row_factory=dict_row)
        self._conn.autocommit = False
        # psycopg3 opens an implicit transaction on the first statement
        # executed while autocommit=False. `SET LOCAL ROLE` is scoped to
        # that transaction and evaporates on commit/rollback, matching
        # fde_mcp.db.tool_transaction's contract: this connection is never
        # at an elevated role once the `with` block exits, whatever the
        # caller did inside it.
        self._conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(self.role)))
        return TrainingDBConnection(self._conn)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._conn is not None
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()


def connect(role: str | None = None, dsn: str | None = None) -> _ConnectCtx:
    """`with connect() as db: db.cursor().execute(...)`.

    Opens one long-lived transaction for the life of the `with` block (batch
    export scripts run a handful of big read queries, not thousands of short
    ones -- there is no per-call statement_timeout reset like the MCP
    server's `tool_transaction`, by design: an export job is allowed to run
    longer than 20s).

    `role` defaults to `get_settings().roles.training_role` when omitted;
    callers that need a different role (`generate_traces.py`'s
    `_discard_episode`, which must run as `fde_rl_rollout` to delete rows
    that role itself wrote) pass it explicitly.
    """
    resolved_role = role if role is not None else get_settings().roles.training_role
    return _ConnectCtx(dsn or resolve_dsn(), resolved_role, autocommit=False)


# ===========================================================================
# Canonicalisation
#
# Every place in this pipeline that hashes or compares tool calls/results
# must use these two functions, and only these two. Two structurally
# identical calls that merely serialise their arguments in a different key
# order (a very common source of "duplicate" trajectories in agent traces,
# since dict key order in Python/JSON is generally insertion order and two
# code paths can construct the same call with keys in different orders) must
# hash identically -- otherwise trajectory dedup and the RL schema-validity
# reward silently under-count duplicates.
# ===========================================================================
def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialisation: sorted keys, no insignificant
    whitespace, floats rendered with `repr`-stable precision via the default
    json float formatter. Used for hashing and for display in exported
    preference pairs.

    `default=str` covers psycopg's native Python return types for uuid/
    timestamptz/numeric columns (uuid.UUID, datetime, Decimal) -- callers
    that read rows straight from `cursor.fetchall()` (rollout_env.py,
    export_sft.py) pass them through this function without a separate
    `jsonify` pass first, same rationale as `fde_mcp.tools._base`'s own
    JSON-safety handling: skipping this quietly breaks exactly the tool
    calls whose results contain ids and timestamps, i.e. almost all of them.
    """
    return json.dumps(_canonicalize(obj), sort_keys=True, separators=(",", ":"), default=str)


def _canonicalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _canonicalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(v) for v in obj]
    return obj


def canonical_tool_call_arguments(raw_arguments: Any) -> str:
    """Tool-call `function.arguments` arrives as either a JSON string (the
    OpenAI wire shape used by trn.trace_step.tool_calls, see
    db/009_training.sql) or, occasionally, an already-parsed dict/list if a
    caller stored it that way. Normalise both to the same canonical JSON
    string so two calls with the same arguments in different key orders
    compare equal.

    Non-JSON-parseable strings are returned unchanged (canonicalisation is a
    best-effort normalisation, not a validator -- schema validation is
    `fde_training.rewards.r_schema_valid`'s job, and export_sft.py must not
    silently swallow a malformed tool call by "fixing" it here).
    """
    if raw_arguments is None:
        return "{}"
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except (json.JSONDecodeError, TypeError):
            return raw_arguments
        return canonical_json(parsed)
    return canonical_json(raw_arguments)


def canonicalize_tool_calls(
    tool_calls: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Return a new tool_calls list with every `function.arguments` string
    replaced by its canonical form. Preserves `id`/`type`/`function.name`.
    """
    if not tool_calls:
        return tool_calls
    out = []
    for tc in tool_calls:
        tc2 = dict(tc)
        fn = dict(tc.get("function") or {})
        if "arguments" in fn:
            fn["arguments"] = canonical_tool_call_arguments(fn["arguments"])
        tc2["function"] = fn
        out.append(tc2)
    return out


def tool_call_signature(tool_name: str | None, canonical_args: str) -> tuple[str, str]:
    return (tool_name or "", canonical_args)


# ===========================================================================
# Volatile-field stripping
#
# Documented policy (required by the task spec): strip fields from tool
# RESULTS that (a) are pure per-run telemetry never referenced as an
# argument to a later tool call in the SAME trajectory, and (b) would
# otherwise teach the model to memorise a number that is different every
# time the same logical trajectory is replayed.
#
# This is intentionally a SHORT, EXPLICIT allowlist-of-what-to-strip rather
# than a heuristic (e.g. "strip anything that looks like a timestamp") --
# see fde_mcp.server's tool result shapes: `proposal_id`, `commit_id`,
# `node_key`, `workflow_id` etc ARE stable/causally-referenced identifiers
# that a later assistant turn in the same trajectory cites verbatim (e.g. a
# `kg_submit_proposal(proposal_id=...)` call reading the `proposal_id` a
# prior `kg_propose` call returned). Stripping those would silently teach the
# model to hallucinate IDs it can no longer see in context -- a much worse
# bug than the one this stripping exists to prevent. Every key below is a
# telemetry/wall-clock field that fde_mcp.server's tool docstrings confirm is
# NEVER read back by a subsequent tool argument.
# ===========================================================================
VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "latency_ms",
        "created_at",
        "started_at",
        "ended_at",
        "decided_at",
        "due_at",
        "triaged_at",
        "detected_at",
        "last_seen_at",
        "extracted_at",
        "embedded_at",
        "captured_at",
        "sealed_at",
        "submitted_at",
        "updated_at",
        "expires_at",
        "granted_at",
        "revoked_at",
        "review_seconds",
        # server.py's own truncation marker for oversized tool results --
        # re-derived by export_sft.py's own truncation policy below, so the
        # original byte count from a *different* run is noise, not signal.
        "_original_bytes",
    }
)


def strip_volatile(obj: Any) -> Any:
    """Recursively drop VOLATILE_KEYS from dicts. Lists/scalars pass through
    (with dict elements recursed into)."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj


# ===========================================================================
# Truncation policy
#
# Documented policy: a tool result that renders to more than
# MAX_RESULT_CHARS characters as canonical JSON is truncated as follows,
# in order:
#   1. If the result has a top-level list-valued field among LIST_FIELDS
#      (the common "returned rows" shape across every kg_* read tool), cap
#      that list at MAX_LIST_ITEMS entries and record how many were dropped.
#   2. If it is still over budget after (1) (e.g. a single huge `summary` or
#      `content` string), hard-truncate the JSON text itself and mark it.
# The truncation marker shape mirrors fde_mcp.server's own
# `_truncate_for_trace` convention (`_truncated`, a count, a `preview`) so a
# downstream consumer only needs to recognise one shape, whether the
# truncation happened at trace-capture time or at export time.
# ===========================================================================
MAX_RESULT_CHARS = 4000
MAX_LIST_ITEMS = 25
LIST_FIELDS = ("results", "nodes", "closure", "impact", "steps", "signals", "workflows")


def truncate_tool_result(
    obj: Any, max_chars: int = MAX_RESULT_CHARS, max_list_items: int = MAX_LIST_ITEMS
) -> Any:
    if not isinstance(obj, dict):
        text = canonical_json(obj)
        if len(text) <= max_chars:
            return obj
        return {"_truncated": True, "_policy": "hard_truncate", "preview": text[:max_chars]}

    out = dict(obj)
    truncated_any = False
    for field_name in LIST_FIELDS:
        val = out.get(field_name)
        if isinstance(val, list) and len(val) > max_list_items:
            dropped = len(val) - max_list_items
            out[field_name] = val[:max_list_items]
            out[f"_{field_name}_truncated"] = dropped
            truncated_any = True

    text = canonical_json(out)
    if len(text) <= max_chars:
        return out

    # Still too big (a huge single string field, most likely `summary` or
    # `content` on a kg_get_node/kg_search result) -- hard truncate the
    # rendered JSON, matching fde_mcp.server's own preview convention.
    return {
        "_truncated": True,
        "_policy": "hard_truncate_after_list_cap" if truncated_any else "hard_truncate",
        "preview": text[:max_chars],
    }


# ===========================================================================
# Trajectory hashing + deterministic split assignment
# ===========================================================================
def trajectory_signature_hash(signatures: Iterable[tuple[str, str]]) -> str:
    """sha256 over the ordered sequence of (tool_name, canonical_args) pairs.
    Two sessions that called the same tools with the same arguments in the
    same order hash identically regardless of session_id, wall-clock, or
    free-text assistant prose -- exactly the near-duplicate trajectories the
    task asks us to dedup (e.g. an agent retried the same question after a
    transient error and produced a second, cosmetically different but
    functionally identical trace).
    """
    h = hashlib.sha256()
    for tool_name, args in signatures:
        h.update(tool_name.encode("utf-8"))
        h.update(b"\x00")
        h.update(args.encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


SPLIT_NAMES = ("train", "validation", "test")


def deterministic_split(
    session_id: str, ratios: tuple[float, float, float] = (0.8, 0.1, 0.1)
) -> str:
    """Stable train/validation/test assignment keyed ONLY on session_id, via
    md5(session_id) mod 10000 bucketed by `ratios`. Deterministic across
    re-exports (no dependency on export order, dataset size, or which other
    sessions exist) and independent of Postgres's own `trn.trace_session
    .split` column so re-running the export never reshuffles a session
    across splits even if the DB-side column is later populated or cleared.

    Callers should prefer an explicit, already-set `trace_session.split`
    when present (human-curated splits, e.g. a deliberately held-out
    engagement) and fall back to this function only when it is NULL -- see
    export_sft.py's `resolve_split`.
    """
    assert abs(sum(ratios) - 1.0) < 1e-6, "split ratios must sum to 1.0"
    digest = hashlib.md5(session_id.encode("utf-8"), usedforsecurity=False).hexdigest()
    bucket = int(digest[:8], 16) % 10_000
    train_cut = int(ratios[0] * 10_000)
    val_cut = train_cut + int(ratios[1] * 10_000)
    if bucket < train_cut:
        return "train"
    if bucket < val_cut:
        return "validation"
    return "test"


# ===========================================================================
# Small shared value objects
# ===========================================================================
@dataclass
class RetrievalTelemetry:
    """Parsed form of trace_step.retrieval (see db/009_training.sql)."""

    k: int | None = None
    returned: int | None = None
    rrf_top: float | None = None
    hops: int | None = None
    lists_matched: dict[str, int] = field(default_factory=dict)
    nodes_visited: int | None = None
    latency_ms: int | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> RetrievalTelemetry:
        raw = raw or {}
        return cls(
            k=raw.get("k"),
            returned=raw.get("returned"),
            rrf_top=raw.get("rrf_top"),
            hops=raw.get("hops"),
            lists_matched=raw.get("lists_matched") or {},
            nodes_visited=raw.get("nodes_visited"),
            latency_ms=raw.get("latency_ms"),
        )


def jsonify(value: Any) -> Any:
    """Recursively coerce psycopg's native Python return types (uuid.UUID,
    datetime, Decimal) into JSON-safe values. Mirrors `fde_mcp.tools._base`'s
    own jsonify handling (same rationale: dict_row hands back native Python
    objects for uuid/timestamptz/numeric columns, and every downstream
    consumer here -- Jsonb(), canonical_json(), hashing -- needs plain
    str/int/float/bool/None/list/dict). Callers that fetch rows directly
    from a cursor (rollout_env.py) must run this before storing or hashing
    them; export_sft.py/common.connect() already return psycopg's parsed
    jsonb columns, which are themselves plain dict/list, so this is mostly
    needed for uuid/timestamptz *columns*, not jsonb payloads.
    """
    if isinstance(value, dict):
        return {k: jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonify(v) for v in value]
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _decimal.Decimal):
        return float(value)
    return value


_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def looks_like_timestamp(value: Any) -> bool:
    return isinstance(value, str) and bool(_TIMESTAMP_RE.match(value))
