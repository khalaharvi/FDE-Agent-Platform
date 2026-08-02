"""adapters/db_cdc.py -- logical decoding from a customer's own Postgres.

The docs/08 §2.4 scenario: a customer runs their operational system on
Postgres and the cheapest, lowest-latency way to observe it is to read its
WAL rather than poll its API.

Why the SQL-level slot functions, not the replication protocol
---------------------------------------------------------------
`pg_logical_slot_peek_changes` / `pg_replication_slot_advance` are ordinary
SQL functions callable over an ordinary connection. Using them instead of
`psycopg.connect(replication=...)` means: no second connection mode to
configure, no streaming-protocol keepalive loop to get wrong, and -- the
decisive one -- the read and the acknowledgement are two separate,
individually-timed statements, so the acknowledgement can be deferred until
after the observations are committed in the PLATFORM database. A streaming
consumer that has already consumed a change when the platform insert fails has
lost it; peek-then-advance simply re-reads it.

The sequence per run is therefore:

  1. `pg_logical_slot_peek_changes(slot, NULL, NULL, ...)` -- read WITHOUT
     consuming.
  2. Map and insert into `sor.observation`, committing per batch.
  3. `pg_replication_slot_advance(slot, lsn)` -- only now, via
     `on_batch_committed`.

That is at-least-once, and the `(adapter_id, dedup_key)` index from db/014
makes it effectively exactly-once.

Customer-side prerequisites (they are real, and they are theirs):
`wal_level = logical`, the `wal2json` output plugin installed, and a role with
REPLICATION. `fde-sor adapter create-slot` creates the slot once at
onboarding. An abandoned slot pins WAL forever and will eventually fill the
customer's disk, so `fde-sor adapter disable` warns about dropping it.

`mapping.cdc`:

    {"slot_name": "fde_sor_tickets",
     "plugin": "wal2json",
     "tables": ["public.tickets"],
     "dsn_from_secret": "dsn"}
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, ClassVar

import psycopg
from psycopg.rows import dict_row

from fde_mcp.logging import get_logger
from fde_sor.adapters.base import RawRecord
from fde_sor.config import aws_region

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = ["DbCdcAdapter", "create_replication_slot", "parse_wal2json"]

log = get_logger(__name__)

# wal2json emits row-level changes for these; DDL and truncates carry no
# columnvalues and are not observations of business activity.
_ROW_CHANGE_KINDS = ("insert", "update")

DEFAULT_PLUGIN = "wal2json"


def parse_wal2json(payload: str | dict[str, Any], tables: frozenset[str]) -> list[dict[str, Any]]:
    """Turn one wal2json format-1 message into a list of flat row dicts.

    wal2json gives `{"change": [{"kind", "schema", "table", "columnnames",
    "columnvalues", ...}]}`; this zips names and values into the ordinary
    record shape the mapping layer expects, and adds `_table` / `_kind` so a
    mapping can address them as dotted paths like any other field.

    Deletes are skipped: wal2json reports them via `oldkeys`, and a deletion is
    not an observation that an activity HAPPENED, which is what
    `sor.observation` records. `tables` empty means "every table".

    Pure, so the parsing -- the part that actually varies with wal2json
    versions -- is fixture-tested without a database, a replication slot, or a
    `wal_level` a CI service container cannot set.
    """
    message = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(message, dict):
        return []
    changes = message.get("change")
    if not isinstance(changes, list):
        return []

    rows: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        if change.get("kind") not in _ROW_CHANGE_KINDS:
            continue
        qualified = f"{change.get('schema', 'public')}.{change.get('table', '')}"
        if tables and qualified not in tables:
            continue
        names = change.get("columnnames")
        values = change.get("columnvalues")
        if not isinstance(names, list) or not isinstance(values, list):
            continue
        row: dict[str, Any] = dict(zip(names, values, strict=False))
        row["_table"] = qualified
        row["_kind"] = change.get("kind")
        rows.append(row)
    return rows


async def _customer_dsn(secret_arn: str | None, dsn_key: str) -> str:
    """Resolve the CUSTOMER database DSN from Secrets Manager.

    Never from `mapping`: `sor.adapter.mapping` is a plain jsonb column that
    anyone with SELECT on the table can read, and this is a connection string
    to someone else's production database.
    """
    if not secret_arn:
        msg = "db_cdc adapters need a secret_arn holding the customer database DSN"
        raise ValueError(msg)
    import boto3  # noqa: PLC0415

    def _fetch() -> str:
        client = boto3.client("secretsmanager", region_name=aws_region())
        return str(client.get_secret_value(SecretId=secret_arn)["SecretString"])

    raw = await asyncio.to_thread(_fetch)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw  # a bare DSN string is a legitimate secret shape
    if isinstance(parsed, dict):
        value = parsed.get(dsn_key)
        if not value:
            msg = f"secret {secret_arn} has no {dsn_key!r} key"
            raise ValueError(msg)
        return str(value)
    return str(parsed)


def create_replication_slot(dsn: str, slot_name: str, plugin: str = DEFAULT_PLUGIN) -> str | None:
    """Create the logical replication slot on the customer database, once.

    Returns the slot's initial LSN, or `None` if it already existed. Called at
    registration (`fde-sor adapter register`), not per run: creating a slot
    per poll would lose every change between runs, which is the opposite of
    what a slot is for.
    """
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_replication_slots WHERE slot_name = %(slot)s", {"slot": slot_name}
        )
        if cur.fetchone() is not None:
            log.info("cdc_slot_exists", slot_name=slot_name)
            return None
        cur.execute(
            "SELECT lsn FROM pg_create_logical_replication_slot(%(slot)s, %(plugin)s)",
            {"slot": slot_name, "plugin": plugin},
        )
        row = cur.fetchone()
    lsn = str(row["lsn"]) if row else None
    log.info("cdc_slot_created", slot_name=slot_name, plugin=plugin, lsn=lsn)
    return lsn


class DbCdcAdapter:
    """Reads a logical replication slot with peek, and advances it on ack."""

    kind: ClassVar[str] = "db_cdc"

    def __init__(
        self,
        cdc_config: dict[str, Any],
        *,
        secret_arn: str | None = None,
        dsn: str | None = None,
    ) -> None:
        slot_name = cdc_config.get("slot_name")
        if not slot_name:
            msg = "db_cdc mapping needs a 'cdc' object with a 'slot_name'"
            raise ValueError(msg)
        self.slot_name = str(slot_name)
        self.plugin = str(cdc_config.get("plugin", DEFAULT_PLUGIN))
        self.tables = frozenset(str(t) for t in cdc_config.get("tables", []))
        self.dsn_key = str(cdc_config.get("dsn_from_secret", "dsn"))
        self.secret_arn = secret_arn
        #: Injectable so the `requires_cdc` test can point at a local database
        #: without inventing a Secrets Manager entry.
        self._dsn = dsn

    async def _resolve_dsn(self) -> str:
        if self._dsn is None:
            self._dsn = await _customer_dsn(self.secret_arn, self.dsn_key)
        return self._dsn

    def _peek(self, dsn: str) -> list[tuple[str, str]]:
        options: list[str] = ["format-version", "1"]
        if self.tables:
            options += ["add-tables", ",".join(sorted(self.tables))]
        with (
            psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT lsn::text AS lsn, data FROM "
                "pg_logical_slot_peek_changes(%(slot)s, NULL, NULL, VARIADIC %(options)s)",
                {"slot": self.slot_name, "options": options},
            )
            return [(str(r["lsn"]), str(r["data"])) for r in cur.fetchall()]

    async def fetch(self, cursor: str | None) -> AsyncIterator[RawRecord]:
        dsn = await self._resolve_dsn()
        changes = await asyncio.to_thread(self._peek, dsn)
        for lsn, data in changes:
            for row in parse_wal2json(data, self.tables):
                yield RawRecord(payload=row, cursor=lsn, ref=lsn)

    async def on_batch_committed(self, cursor: str | None) -> None:
        """Advance the slot past `cursor`, releasing the customer's WAL.

        Only reachable after the batch's observations are committed on our
        side -- see the module docstring. A failure here is logged and NOT
        raised: the observations are already durable, and the next run simply
        re-peeks from the un-advanced position and re-inserts rows the dedup
        index throws away.
        """
        if cursor is None:
            return
        dsn = await self._resolve_dsn()

        def _advance() -> None:
            with (
                psycopg.connect(dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    "SELECT pg_replication_slot_advance(%(slot)s, %(lsn)s::pg_lsn)",
                    {"slot": self.slot_name, "lsn": cursor},
                )

        try:
            await asyncio.to_thread(_advance)
        except psycopg.Error:
            log.warning("cdc_slot_advance_failed", slot_name=self.slot_name, lsn=cursor)
            return
        log.info("cdc_slot_advanced", slot_name=self.slot_name, lsn=cursor)
