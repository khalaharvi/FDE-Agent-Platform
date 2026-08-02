"""registry.py -- loading `sor.adapter` rows, and the one place they are written.

Reading is a runtime concern (`fde_ingest` has SELECT on `sor.adapter`).
Writing is not.

Why registration is an operator command, not a tool
----------------------------------------------------
`fde_ingest` deliberately has no INSERT on `sor.adapter`, and db/014 lists
that omission as a decision rather than an oversight. The reason is in
docs/08 §2.5: the mapping IS the measurement instrument. A runtime role that
could register its own adapter could also define away the drift it is measured
by -- change one `activity_map` entry and a `control_bypass` finding
disappears, with no proposal, no gate, and no reviewer. So `register_adapter`
connects with the OPERATOR DSN via a plain `psycopg.connect` (the same posture
as `db/rebuild.sh` and the pytest seed fixture), and a test asserts that
`fde_ingest` attempting the same INSERT is denied by Postgres.

Registration validates the mapping with `MappingSpec.parse` FIRST, so a typo
is caught at onboarding by the person who can fix it, rather than at 3am by a
Lambda.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fde_mcp import db
from fde_mcp.logging import get_logger
from fde_sor.adapters.base import AdapterRow
from fde_sor.adapters.db_cdc import DbCdcAdapter
from fde_sor.adapters.event_stream import SqsPollingAdapter
from fde_sor.adapters.replay import ReplayAdapter
from fde_sor.adapters.rest_poll import RestPollAdapter
from fde_sor.config import get_settings
from fde_sor.mapping import MappingSpec

if TYPE_CHECKING:
    from fde_sor.adapters.base import SorAdapter

__all__ = [
    "ADAPTER_KINDS",
    "AdapterNotFoundError",
    "active_engagement_ids",
    "build_adapter",
    "disable_adapter",
    "list_adapters",
    "load_adapter",
    "load_adapter_by_queue",
    "register_adapter",
]

log = get_logger(__name__)

#: Mirrors `sor.adapter`'s own CHECK constraint (db/007:30-31). `webhook` and
#: `warehouse_query` are registrable but have no fetch implementation yet;
#: `build_adapter` says so explicitly rather than failing with an AttributeError.
ADAPTER_KINDS = ("rest_poll", "webhook", "event_stream", "db_cdc", "warehouse_query")
IMPLEMENTED_KINDS = ("rest_poll", "event_stream", "db_cdc")

_SELECT_COLUMNS = (
    "adapter_id, engagement_id::text AS engagement_id, adapter_key, system_node_key, "
    "kind, secret_arn, mapping, last_cursor, poll_cron, is_active"
)


class AdapterNotFoundError(LookupError):
    """No `sor.adapter` row matched."""


def _to_row(record: dict[str, Any]) -> AdapterRow:
    return AdapterRow(
        adapter_id=record["adapter_id"],
        engagement_id=str(record["engagement_id"]),
        adapter_key=record["adapter_key"],
        system_node_key=record["system_node_key"],
        kind=record["kind"],
        secret_arn=record["secret_arn"],
        mapping=dict(record["mapping"]),
        last_cursor=record["last_cursor"],
        poll_cron=record["poll_cron"],
        is_active=record["is_active"],
    )


async def load_adapter(engagement_id: str, adapter_key: str) -> AdapterRow:
    """Load one adapter by its natural key `(engagement_id, adapter_key)`."""
    settings = get_settings()
    async with db.tool_transaction(role=settings.role) as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_SELECT_COLUMNS} FROM sor.adapter "  # noqa: S608 -- constant column list, no interpolation of caller input
            "WHERE engagement_id = %(eng)s::uuid AND adapter_key = %(key)s",
            {"eng": engagement_id, "key": adapter_key},
        )
        record = await cur.fetchone()
    if record is None:
        msg = f"no sor.adapter {adapter_key!r} registered for engagement {engagement_id}"
        raise AdapterNotFoundError(msg)
    return _to_row(record)


async def load_adapter_by_queue(queue_arn: str) -> AdapterRow:
    """Find the event_stream adapter whose `mapping.stream.queue_url` matches
    an SQS event-source ARN.

    This is what lets ONE `fde-sor-stream` Lambda serve every event_stream
    adapter: the event-source mapping tells us which queue delivered the
    batch, and the queue identifies the adapter. The alternative -- an
    `FDE_SOR_ADAPTER_KEY` environment variable -- would need one Lambda per
    adapter, which is one deployment per customer integration.

    Matched on the queue NAME (the ARN's last segment) because an ARN and a
    queue URL are different spellings of the same queue.
    """
    queue_name = queue_arn.rsplit(":", 1)[-1]
    settings = get_settings()
    async with db.tool_transaction(role=settings.role) as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_SELECT_COLUMNS} FROM sor.adapter "  # noqa: S608 -- constant column list
            "WHERE kind = 'event_stream' AND is_active "
            "AND mapping #>> '{stream,queue_url}' LIKE %(pattern)s",
            {"pattern": f"%/{queue_name}"},
        )
        records = await cur.fetchall()
    if not records:
        msg = f"no active event_stream sor.adapter has a stream.queue_url ending in /{queue_name}"
        raise AdapterNotFoundError(msg)
    if len(records) > 1:
        keys = sorted(r["adapter_key"] for r in records)
        msg = f"queue {queue_name} matches {len(records)} adapters ({keys}); adapter_keys must be unambiguous"
        raise AdapterNotFoundError(msg)
    return _to_row(records[0])


async def list_adapters(
    *, engagement_id: str | None = None, active_only: bool = True
) -> list[AdapterRow]:
    settings = get_settings()
    async with db.tool_transaction(role=settings.role) as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_SELECT_COLUMNS} FROM sor.adapter "  # noqa: S608 -- constant column list
            "WHERE (%(eng)s::uuid IS NULL OR engagement_id = %(eng)s::uuid) "
            "  AND (NOT %(active_only)s OR is_active) "
            "ORDER BY engagement_id, adapter_key",
            {"eng": engagement_id, "active_only": active_only},
        )
        records = await cur.fetchall()
    return [_to_row(r) for r in records]


async def active_engagement_ids() -> list[str]:
    """Every engagement with at least one active adapter.

    The drift scan iterates these rather than "every engagement that exists":
    an engagement with no adapter has no observations, so every detector would
    return zero and the only effect would be a `REFRESH MATERIALIZED VIEW` per
    empty engagement per six hours.
    """
    settings = get_settings()
    async with db.tool_transaction(role=settings.role) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT DISTINCT engagement_id::text AS engagement_id FROM sor.adapter "
            "WHERE is_active ORDER BY 1"
        )
        return [str(r["engagement_id"]) for r in await cur.fetchall()]


def build_adapter(
    row: AdapterRow, *, source_uri: str | None = None, dsn: str | None = None
) -> SorAdapter:
    """Construct the adapter implementation for a registered row.

    `source_uri` overrides the kind entirely and returns a `ReplayAdapter`:
    a backfill from a JSONL export runs the same mapping, hashing and insert
    path as the live source it is backfilling, which is the only way the two
    can be relied on to produce identical rows (and therefore for the dedup
    index to recognise the overlap at the seam).
    """
    if source_uri is not None:
        return ReplayAdapter(source_uri)
    spec = MappingSpec.parse(row.mapping)
    if row.kind == "rest_poll":
        return RestPollAdapter(spec, secret_arn=row.secret_arn)
    if row.kind == "event_stream":
        return SqsPollingAdapter(spec.stream)
    if row.kind == "db_cdc":
        return DbCdcAdapter(spec.cdc, secret_arn=row.secret_arn, dsn=dsn)
    msg = (
        f"sor.adapter kind {row.kind!r} has no fetch implementation "
        f"(implemented: {IMPLEMENTED_KINDS}). Backfill it from an export with "
        "`fde-sor backfill --s3-uri ...` in the meantime."
    )
    raise NotImplementedError(msg)


# ---------------------------------------------------------------------------
# Operator-side writes. Owner DSN, plain psycopg, no SET LOCAL ROLE downgrade.
# ---------------------------------------------------------------------------
def register_adapter(
    dsn: str,
    *,
    engagement_id: str,
    adapter_key: str,
    system_node_key: str,
    kind: str,
    mapping: dict[str, Any],
    secret_arn: str | None = None,
    poll_cron: str | None = None,
) -> dict[str, Any]:
    """Insert one `sor.adapter` row. Operator command -- see module docstring.

    Validates the mapping first, and warns (does not fail) when
    `system_node_key` is not a live node: the graph and the adapters are often
    onboarded in parallel, and refusing to register an adapter because its
    system node has not been merged yet would serialise two things that do not
    need to be.
    """
    if kind not in ADAPTER_KINDS:
        msg = f"kind must be one of {ADAPTER_KINDS!r}, got {kind!r}"
        raise ValueError(msg)
    MappingSpec.parse(mapping)  # raises MappingError naming every problem

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM kg.node_current WHERE engagement_id = %(eng)s::uuid AND node_key = %(key)s",
            {"eng": engagement_id, "key": system_node_key},
        )
        if cur.fetchone() is None:
            log.warning(
                "adapter_system_node_not_in_graph",
                engagement_id=engagement_id,
                system_node_key=system_node_key,
                note=(
                    "the adapter will register, but drift detectors join "
                    "observations to graph nodes -- merge this system node "
                    "before expecting signals"
                ),
            )
        cur.execute(
            """
            INSERT INTO sor.adapter
                (engagement_id, adapter_key, system_node_key, kind, secret_arn,
                 mapping, poll_cron)
            VALUES (%(eng)s::uuid, %(key)s, %(node)s, %(kind)s, %(secret)s,
                    %(mapping)s, %(cron)s)
            RETURNING adapter_id, adapter_key, kind, poll_cron, is_active, created_at
            """,
            {
                "eng": engagement_id,
                "key": adapter_key,
                "node": system_node_key,
                "kind": kind,
                "secret": secret_arn,
                "mapping": Jsonb(mapping),
                "cron": poll_cron,
            },
        )
        record = cur.fetchone()
    assert record is not None, "RETURNING always yields exactly one row here"
    log.info(
        "adapter_registered",
        engagement_id=engagement_id,
        adapter_key=adapter_key,
        kind=kind,
        adapter_id=record["adapter_id"],
    )
    return dict(record)


def disable_adapter(dsn: str, *, engagement_id: str, adapter_key: str) -> dict[str, Any] | None:
    """Set `is_active = false`. Operator command.

    Never a DELETE: `sor.observation.adapter_id` is a foreign key, and the
    observations an adapter produced remain the evidence behind every drift
    signal it ever raised.
    """
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE sor.adapter SET is_active = false "
            "WHERE engagement_id = %(eng)s::uuid AND adapter_key = %(key)s "
            "RETURNING adapter_id, adapter_key, kind, is_active, mapping",
            {"eng": engagement_id, "key": adapter_key},
        )
        record = cur.fetchone()
    if record is None:
        return None
    if record["kind"] == "db_cdc":
        slot = json.loads(json.dumps(record["mapping"])).get("cdc", {}).get("slot_name")
        log.warning(
            "cdc_adapter_disabled_slot_still_exists",
            adapter_key=adapter_key,
            slot_name=slot,
            note=(
                "an abandoned logical replication slot pins WAL on the "
                "CUSTOMER's database until it is dropped, and will eventually "
                "fill their disk. Drop it with "
                "SELECT pg_drop_replication_slot('<slot>') once you are sure."
            ),
        )
    return dict(record)
