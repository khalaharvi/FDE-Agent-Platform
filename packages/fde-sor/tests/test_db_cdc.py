"""Tests for the db_cdc adapter.

Split deliberately in two. The wal2json PARSING -- the part that varies with
plugin versions and is where a mistake silently drops changes -- is a pure
function tested from fixtures, with no database at all. The live slot
end-to-end is marked `requires_cdc` and self-skips: CI's pgvector image has no
wal2json plugin, and a service container cannot be started with
`wal_level=logical`, so that test is honestly skipped there rather than
quietly deleted or made to pass against a mock.

Note for the integrator: `requires_cdc` is registered in this package's
`tests/conftest.py` via `pytest_configure` so `--strict-markers` accepts it
before the marker reaches the root `pyproject.toml` marker list.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import psycopg
import pytest

from fde_sor.adapters.db_cdc import DbCdcAdapter, create_replication_slot, parse_wal2json


def dsn() -> str:
    return os.environ["FDE_DB_DSN"]


# One wal2json format-version-1 message, as the plugin actually emits it.
WAL2JSON_MESSAGE: dict[str, Any] = {
    "change": [
        {
            "kind": "insert",
            "schema": "public",
            "table": "tickets",
            "columnnames": ["id", "case_key", "status", "assignee", "updated_at"],
            "columntypes": ["integer", "text", "text", "text", "timestamptz"],
            "columnvalues": [1, "CPQ-1", "In Review", "acct-1", "2026-03-01 10:00:00+00"],
        },
        {
            "kind": "update",
            "schema": "public",
            "table": "tickets",
            "columnnames": ["id", "case_key", "status", "assignee", "updated_at"],
            "columntypes": ["integer", "text", "text", "text", "timestamptz"],
            "columnvalues": [1, "CPQ-1", "Deal Desk Approved", "acct-1", "2026-03-01 11:00:00+00"],
            "oldkeys": {"keynames": ["id"], "keyvalues": [1]},
        },
        {
            "kind": "delete",
            "schema": "public",
            "table": "tickets",
            "oldkeys": {"keynames": ["id"], "keyvalues": [2]},
        },
        {
            "kind": "insert",
            "schema": "public",
            "table": "audit_log",
            "columnnames": ["id", "note"],
            "columntypes": ["integer", "text"],
            "columnvalues": [9, "unrelated table"],
        },
    ]
}


# ===========================================================================
# Pure parsing
# ===========================================================================
def test_parse_zips_column_names_and_values_into_flat_records() -> None:
    rows = parse_wal2json(WAL2JSON_MESSAGE, frozenset({"public.tickets"}))
    assert len(rows) == 2
    assert rows[0]["case_key"] == "CPQ-1"
    assert rows[0]["status"] == "In Review"
    assert rows[1]["status"] == "Deal Desk Approved"


def test_parse_adds_addressable_table_and_kind_fields() -> None:
    """`_table` and `_kind` are ordinary dotted-path fields, so a mapping can
    branch on them the same way it addresses anything else.
    """
    rows = parse_wal2json(WAL2JSON_MESSAGE, frozenset({"public.tickets"}))
    assert rows[0]["_table"] == "public.tickets"
    assert rows[0]["_kind"] == "insert"
    assert rows[1]["_kind"] == "update"


def test_parse_skips_deletes() -> None:
    """A deletion is not an observation that an activity HAPPENED, which is
    what sor.observation records -- and wal2json reports it with no
    columnvalues to map anyway.
    """
    rows = parse_wal2json(WAL2JSON_MESSAGE, frozenset({"public.tickets"}))
    assert all(r["_kind"] != "delete" for r in rows)


def test_parse_filters_to_the_configured_tables() -> None:
    rows = parse_wal2json(WAL2JSON_MESSAGE, frozenset({"public.tickets"}))
    assert all(r["_table"] == "public.tickets" for r in rows)


def test_parse_with_no_table_filter_keeps_every_row_change() -> None:
    rows = parse_wal2json(WAL2JSON_MESSAGE, frozenset())
    assert {r["_table"] for r in rows} == {"public.tickets", "public.audit_log"}


def test_parse_accepts_the_raw_json_string_form() -> None:
    """`pg_logical_slot_peek_changes` returns `data` as text, so the string
    path is the one production actually takes.
    """
    rows = parse_wal2json(json.dumps(WAL2JSON_MESSAGE), frozenset({"public.tickets"}))
    assert len(rows) == 2


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"change": []},
        {"change": "not a list"},
        {"change": [{"kind": "insert", "schema": "public", "table": "t"}]},  # no columns
        [1, 2, 3],
    ],
)
def test_parse_tolerates_every_degenerate_message_shape(message: Any) -> None:
    assert parse_wal2json(message, frozenset()) == []


def test_a_cdc_block_without_a_slot_name_is_refused() -> None:
    with pytest.raises(ValueError, match="slot_name"):
        DbCdcAdapter({})


# ===========================================================================
# Live logical decoding
# ===========================================================================
def _logical_decoding_available() -> tuple[bool, str]:
    """Probe the configured database for wal_level=logical AND wal2json.

    Probing rather than assuming: the same test file runs on a developer's
    local Postgres (where both are often present) and in CI (where neither
    is), and a test that fails in one and passes in the other teaches nobody
    anything.
    """
    if not os.environ.get("FDE_DB_DSN"):
        return False, "FDE_DB_DSN not set"
    try:
        with psycopg.connect(dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SHOW wal_level")
            row = cur.fetchone()
            if row is None or row[0] != "logical":
                return False, f"wal_level is {row[0] if row else 'unknown'}, not logical"
            cur.execute("SELECT 1 FROM pg_get_available_extensions() WHERE name = 'wal2json'")
            plugin_as_extension = cur.fetchone() is not None
            probe_slot = f"fde_sor_probe_{uuid.uuid4().hex[:8]}"
            try:
                cur.execute(
                    "SELECT pg_create_logical_replication_slot(%s, 'wal2json')", (probe_slot,)
                )
            except psycopg.Error as exc:
                return False, f"wal2json slot not creatable ({type(exc).__name__})"
            cur.execute("SELECT pg_drop_replication_slot(%s)", (probe_slot,))
            return True, f"available (extension listed: {plugin_as_extension})"
    except psycopg.Error as exc:
        return False, f"probe failed: {type(exc).__name__}"


_CDC_AVAILABLE, _CDC_REASON = _logical_decoding_available()

# Applied per test, NOT as a module-level `pytestmark`: everything above this
# line is pure parsing and must run everywhere, including in CI where logical
# decoding is unavailable. Marking the whole module would have silently
# skipped the wal2json coverage that is the actual safety net here.
requires_cdc = pytest.mark.requires_cdc
skip_without_cdc = pytest.mark.skipif(
    not _CDC_AVAILABLE, reason=f"logical decoding unavailable: {_CDC_REASON}"
)


@pytest.fixture
def cdc_table() -> Any:
    """A throwaway table plus a slot, dropped together afterwards."""
    table = f"fde_sor_cdc_{uuid.uuid4().hex[:8]}"
    slot = f"fde_sor_slot_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE public.{table} "
            "(id serial PRIMARY KEY, case_key text, status text, updated_at timestamptz)"
        )
    create_replication_slot(dsn(), slot)
    try:
        yield {"table": table, "slot": slot, "qualified": f"public.{table}"}
    finally:
        with psycopg.connect(dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_drop_replication_slot(%s)", (slot,))
            cur.execute(f"DROP TABLE IF EXISTS public.{table}")


@requires_cdc
@skip_without_cdc
async def test_live_slot_yields_inserted_rows_and_advances_only_on_ack(
    cdc_table: dict[str, str],
) -> None:
    with psycopg.connect(dsn(), autocommit=True) as conn, conn.cursor() as cur:
        for i in range(3):
            cur.execute(
                f"INSERT INTO public.{cdc_table['table']} (case_key, status, updated_at) "  # noqa: S608 -- generated name
                "VALUES (%s, 'In Review', now())",
                (f"CPQ-{i}",),
            )

    adapter = DbCdcAdapter(
        {"slot_name": cdc_table["slot"], "tables": [cdc_table["qualified"]]}, dsn=dsn()
    )

    first = [r async for r in adapter.fetch(None)]
    assert len(first) == 3
    assert {r.payload["case_key"] for r in first} == {"CPQ-0", "CPQ-1", "CPQ-2"}

    # Peek does not consume: a second fetch before any ack sees the same
    # changes, which is exactly what makes a crash before commit recoverable.
    second = [r async for r in adapter.fetch(None)]
    assert len(second) == 3, "peek must not consume"

    await adapter.on_batch_committed(first[-1].cursor)

    third = [r async for r in adapter.fetch(None)]
    assert third == [], "after the ack the slot must be past those changes"
