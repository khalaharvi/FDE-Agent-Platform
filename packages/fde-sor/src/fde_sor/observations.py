"""observations.py -- the one pipeline every adapter kind runs through.

`ingest(adapter, row)` is the only place in this repo that writes
`sor.observation`. Per-kind modules contribute `fetch`; mapping, actor
hashing, the idempotent insert and the cursor advance happen here, once. If
that logic were per-kind, "how a Jira poll interprets `activity_map`" and "how
an SQS consumer interprets `activity_map`" would be two implementations that
drift, and the detectors compare their outputs as if they were one.

The transaction boundary is the design
---------------------------------------
Each batch is ONE transaction containing both the inserts AND
`UPDATE sor.adapter SET last_cursor = ..., last_synced_at = now()`. That is
what makes crash-resume correct: a crash between batches resumes from the last
committed cursor, and the records the crashed batch had already fetched are
re-fetched and absorbed by the `(adapter_id, dedup_key)` partial unique index
from db/014. Splitting the two -- inserting, then advancing in a second
transaction -- reintroduces exactly the double-counting that migration exists
to prevent, and `detect_control_bypass` computes a RATE over counted rows, so a
replayed batch does not merely add noise, it moves the number the severity
threshold is read from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from fde_mcp import db
from fde_mcp.logging import get_logger
from fde_sor.adapters.base import AckingAdapter
from fde_sor.config import get_settings
from fde_sor.hashing import resolve_actor_hasher
from fde_sor.mapping import MappingSpec, RecordError, normalize

if TYPE_CHECKING:
    from collections.abc import Callable

    from fde_sor.adapters.base import AdapterRow, SorAdapter
    from fde_sor.mapping import NormalizedObservation

__all__ = ["IngestStats", "ingest"]

log = get_logger(__name__)

_INSERT_SQL = """
INSERT INTO sor.observation
    (engagement_id, adapter_id, case_ref, activity_key, raw_activity,
     actor_hash, actor_role_key, system_object_key, occurred_at,
     duration_seconds, attributes, dedup_key)
SELECT %(eng)s::uuid, %(adapter_id)s, x.case_ref, x.activity_key, x.raw_activity,
       x.actor_hash, x.actor_role_key, x.system_object_key, x.occurred_at,
       x.duration_seconds, COALESCE(x.attributes, '{}'::jsonb), x.dedup_key
  FROM jsonb_to_recordset(%(rows)s::jsonb)
    AS x(case_ref text, activity_key text, raw_activity text, actor_hash text,
         actor_role_key text, system_object_key text, occurred_at timestamptz,
         duration_seconds int, attributes jsonb, dedup_key text)
ON CONFLICT (adapter_id, dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING
"""

# COALESCE so a source with no record-derived watermark (SQS) still gets its
# last_synced_at bumped without blanking a cursor another run set.
_ADVANCE_SQL = """
UPDATE sor.adapter
   SET last_cursor = COALESCE(%(cursor)s, last_cursor),
       last_synced_at = now()
 WHERE adapter_id = %(adapter_id)s
"""


@dataclass(frozen=True, slots=True)
class IngestStats:
    """What one `ingest` run did. Logged as one `sor_ingest_complete` event
    and returned to the Lambda/CLI caller verbatim.
    """

    adapter_key: str
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    unmapped_activities: int = 0
    batches: int = 0
    last_cursor: str | None = None
    record_errors: tuple[str, ...] = ()
    #: `RawRecord.ref` for every record that failed to normalise. The SQS
    #: Lambda turns these into `batchItemFailures`; other kinds ignore them.
    failed_refs: tuple[str, ...] = ()
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter_key": self.adapter_key,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "unmapped_activities": self.unmapped_activities,
            "batches": self.batches,
            "last_cursor": self.last_cursor,
            "record_errors": list(self.record_errors),
            "failed_refs": list(self.failed_refs),
            "dry_run": self.dry_run,
        }


@dataclass(slots=True)
class _Accumulator:
    """Mutable run state. Separate from the frozen `IngestStats` so the
    returned value is a snapshot nobody can edit after the fact.
    """

    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    unmapped_activities: int = 0
    batches: int = 0
    last_cursor: str | None = None
    record_errors: list[str] = field(default_factory=list)
    failed_refs: list[str] = field(default_factory=list)

    def note_error(self, message: str, ref: str | None, *, limit: int) -> None:
        if ref is not None:
            self.failed_refs.append(ref)
        if len(self.record_errors) < limit:
            self.record_errors.append(message)
        elif len(self.record_errors) == limit:
            self.record_errors.append(f"... further record errors suppressed (limit {limit})")


def _row_payload(observation: NormalizedObservation) -> dict[str, Any]:
    """One observation as JSON-safe primitives for `jsonb_to_recordset`.

    `occurred_at` becomes an ISO-8601 string because `Jsonb` serialises with
    plain `json.dumps`, which raises on `datetime` -- the same trap
    `fde_mcp.tools._base.emit_trace` documents. Postgres casts the string back
    to `timestamptz` inside the statement.
    """
    return {
        "case_ref": observation.case_ref,
        "activity_key": observation.activity_key,
        "raw_activity": observation.raw_activity,
        "actor_hash": observation.actor_hash,
        "actor_role_key": observation.actor_role_key,
        "system_object_key": observation.system_object_key,
        "occurred_at": observation.occurred_at.isoformat(),
        "duration_seconds": observation.duration_seconds,
        "attributes": observation.attributes,
        "dedup_key": observation.dedup_key,
    }


async def _flush(
    row: AdapterRow,
    batch: list[NormalizedObservation],
    cursor: str | None,
    acc: _Accumulator,
    *,
    advance_cursor: bool,
) -> None:
    """Commit one batch: the inserts and the cursor advance, together.

    See the module docstring for why these two statements may not be split
    across transactions.

    `advance_cursor=False` still bumps `last_synced_at` but leaves
    `last_cursor` alone. That is the backfill case: a replay's cursor is a
    line number in an export file, and writing it over a rest_poll adapter's
    watermark would make the next live poll ask the SoR for everything
    changed since "4999".
    """
    settings = get_settings()
    payload = [_row_payload(o) for o in batch]
    async with (
        db.tool_transaction(
            role=settings.role, statement_timeout=settings.statement_timeout
        ) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            _INSERT_SQL,
            {"eng": row.engagement_id, "adapter_id": row.adapter_id, "rows": Jsonb(payload)},
        )
        inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        await cur.execute(
            _ADVANCE_SQL,
            {
                "cursor": cursor if advance_cursor else None,
                "adapter_id": row.adapter_id,
            },
        )

    acc.inserted += inserted
    acc.duplicates += len(batch) - inserted
    acc.batches += 1
    if cursor is not None:
        acc.last_cursor = cursor


async def ingest(
    adapter: SorAdapter,
    row: AdapterRow,
    *,
    batch_size: int | None = None,
    dry_run: bool = False,
    advance_cursor: bool = True,
) -> IngestStats:
    """Fetch, normalise, and idempotently insert observations for one adapter.

    `dry_run=True` runs the entire mapping and hashing path and reports what
    WOULD land without writing anything or advancing the cursor -- the way to
    check a freshly authored mapping against real records before it starts
    producing drift signals a reviewer has to trust.

    `advance_cursor=False` writes the observations but leaves
    `sor.adapter.last_cursor` untouched; see `_flush`.
    """
    settings = get_settings()
    size = batch_size or settings.batch_size

    # Both of these happen before the first fetch, on purpose. A mapping that
    # cannot be parsed, or an actor_field with no resolvable salt, must not
    # cost a single call to the customer's system -- and must certainly not
    # fail halfway through a batch, after some records are already normalised.
    spec = MappingSpec.parse(row.mapping)
    hasher: Callable[[str], str] | None = await resolve_actor_hasher(spec, row.engagement_id)

    acc = _Accumulator(last_cursor=row.last_cursor)
    batch: list[NormalizedObservation] = []
    batch_cursor: str | None = None

    async for record in adapter.fetch(row.last_cursor):
        acc.fetched += 1
        try:
            observation = normalize(spec, record.payload, actor_hasher=hasher)
        except RecordError as exc:
            # Bounded, field-names-only (see mapping.RecordError). One bad
            # record never aborts a batch -- a SoR that emits one malformed
            # event an hour would otherwise stop all ingestion for that
            # engagement.
            acc.note_error(str(exc), record.ref, limit=settings.max_record_errors)
            continue
        if observation.activity_key is None:
            acc.unmapped_activities += 1
        batch.append(observation)
        if record.cursor is not None:
            batch_cursor = record.cursor

        if len(batch) >= size:
            if not dry_run:
                await _flush(row, batch, batch_cursor, acc, advance_cursor=advance_cursor)
                if isinstance(adapter, AckingAdapter):
                    await adapter.on_batch_committed(batch_cursor)
            else:
                acc.batches += 1
                acc.last_cursor = batch_cursor or acc.last_cursor
            batch = []

    if batch:
        if not dry_run:
            await _flush(row, batch, batch_cursor, acc, advance_cursor=advance_cursor)
            if isinstance(adapter, AckingAdapter):
                await adapter.on_batch_committed(batch_cursor)
        else:
            acc.batches += 1
            acc.last_cursor = batch_cursor or acc.last_cursor

    stats = IngestStats(
        adapter_key=row.adapter_key,
        fetched=acc.fetched,
        inserted=acc.inserted,
        duplicates=acc.duplicates,
        unmapped_activities=acc.unmapped_activities,
        batches=acc.batches,
        last_cursor=acc.last_cursor,
        record_errors=tuple(acc.record_errors),
        failed_refs=tuple(acc.failed_refs),
        dry_run=dry_run,
    )
    log.info(
        "sor_ingest_complete",
        engagement_id=row.engagement_id,
        adapter_key=row.adapter_key,
        adapter_kind=row.kind,
        fetched=stats.fetched,
        inserted=stats.inserted,
        duplicates=stats.duplicates,
        unmapped_activities=stats.unmapped_activities,
        record_errors=len(stats.record_errors),
        batches=stats.batches,
        last_cursor=stats.last_cursor,
        dry_run=dry_run,
    )
    return stats
