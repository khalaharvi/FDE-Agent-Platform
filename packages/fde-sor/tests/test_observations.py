"""Tests for the shared ingestion pipeline, against a live Postgres.

The replay adapter is the vehicle throughout: it is deterministic, needs no
credentials, and -- crucially -- runs the SAME `observations.ingest` that
rest_poll, event_stream and db_cdc run, so what is asserted here about
mapping, idempotency and cursor handling is true of every kind.

The four properties that matter, and why:

* **columns land as mapped** -- the detectors read these columns directly;
* **replaying twice inserts once** -- db/014's whole reason for existing.
  `detect_control_bypass` computes a RATE over counted rows, so a duplicated
  batch does not add noise, it moves the number the severity threshold reads;
* **the cursor advances in the same transaction as the inserts** -- otherwise
  a crash between them replays or skips a batch;
* **a crash mid-run resumes without duplicating** -- the two above, together,
  under the failure they were designed for.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fde_mcp import db
from fde_sor import registry
from fde_sor.adapters.base import AdapterRow, RawRecord
from fde_sor.adapters.replay import ReplayAdapter
from fde_sor.backfill import backfill
from fde_sor.observations import ingest

pytestmark = pytest.mark.requires_db


def _dsn() -> str:
    return os.environ["FDE_DB_DSN"]


def _query(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _observations(adapter_id: int) -> list[dict[str, Any]]:
    return _query(
        "SELECT * FROM sor.observation WHERE adapter_id = %(aid)s ORDER BY case_ref, occurred_at",
        {"aid": adapter_id},
    )


@pytest.fixture
def adapter_row(seed: dict[str, Any], jira_mapping: dict[str, Any]) -> AdapterRow:
    """A private adapter per test, so counts are never shared between tests."""
    key = f"replay-{uuid.uuid4().hex[:8]}"
    record = registry.register_adapter(
        _dsn(),
        engagement_id=seed["engagement_id"],
        adapter_key=key,
        system_node_key="sys.jira",
        kind="rest_poll",
        mapping=jira_mapping,
        poll_cron="*/15 * * * *",
    )
    return AdapterRow(
        adapter_id=record["adapter_id"],
        engagement_id=seed["engagement_id"],
        adapter_key=key,
        system_node_key="sys.jira",
        kind="rest_poll",
        secret_arn=None,
        mapping=jira_mapping,
        last_cursor=None,
        poll_cron="*/15 * * * *",
    )


@pytest.fixture
def three_records(make_record: Any) -> list[dict[str, Any]]:
    return [
        make_record("CPQ-1", "In Review", "2026-03-01T10:00:00Z"),
        make_record("CPQ-1", "Deal Desk Approved", "2026-03-01T11:00:00Z"),
        make_record("CPQ-2", "In Review", "2026-03-02T10:00:00Z"),
    ]


# ===========================================================================
# Mapping through to columns
# ===========================================================================
async def test_replay_lands_mapped_observations(
    adapter_row: AdapterRow,
    tmp_path: Path,
    three_records: list[dict[str, Any]],
    write_jsonl: Any,
) -> None:
    uri = write_jsonl(tmp_path / "export.jsonl", three_records)
    stats = await ingest(ReplayAdapter(uri), adapter_row)

    assert stats.fetched == 3
    assert stats.inserted == 3
    assert stats.duplicates == 0

    rows = _observations(adapter_row.adapter_id)
    assert len(rows) == 3
    first = rows[0]
    assert first["case_ref"] == "CPQ-1"
    assert first["activity_key"] == "act.legal_review"
    assert first["raw_activity"] == "In Review"
    assert first["actor_role_key"] == "role.deal_desk"
    assert first["system_object_key"] == "CPQ"
    assert first["attributes"] == {"fields.priority": "High"}
    assert first["dedup_key"], "adapters must always populate dedup_key"
    assert first["actor_hash"], "an adapter with an actor_field always hashes"
    assert first["actor_hash"] != "acct-deal-desk", (
        "actor_hash must be a hash, never the raw identifier"
    )


async def test_unmapped_activity_lands_with_null_activity_key(
    adapter_row: AdapterRow,
    tmp_path: Path,
    make_record: Any,
    write_jsonl: Any,
) -> None:
    """This is the `missing_in_graph` signal: the row is kept with its raw
    value so a reviewer can see WHICH SoR value the graph does not model.
    """
    uri = write_jsonl(
        tmp_path / "unmapped.jsonl",
        [make_record("CPQ-9", "Escalated to VP", "2026-03-01T10:00:00Z")],
    )
    stats = await ingest(ReplayAdapter(uri), adapter_row)

    assert stats.unmapped_activities == 1
    rows = _observations(adapter_row.adapter_id)
    assert rows[0]["activity_key"] is None
    assert rows[0]["raw_activity"] == "Escalated to VP"


async def test_a_malformed_record_is_skipped_not_fatal(
    adapter_row: AdapterRow,
    tmp_path: Path,
    make_record: Any,
    write_jsonl: Any,
) -> None:
    uri = write_jsonl(
        tmp_path / "mixed.jsonl",
        [
            make_record("CPQ-1", "In Review", "2026-03-01T10:00:00Z"),
            {"key": "CPQ-2", "fields": {"status": {"name": "In Review"}}},  # no timestamp
            make_record("CPQ-3", "In Review", "2026-03-03T10:00:00Z"),
        ],
    )
    stats = await ingest(ReplayAdapter(uri), adapter_row)

    assert stats.fetched == 3
    assert stats.inserted == 2, "one bad record must not cost the other two"
    assert len(stats.record_errors) == 1
    assert "fields.updated" in stats.record_errors[0], "the error names the field to fix"


async def test_dry_run_writes_nothing(
    adapter_row: AdapterRow,
    tmp_path: Path,
    three_records: list[dict[str, Any]],
    write_jsonl: Any,
) -> None:
    uri = write_jsonl(tmp_path / "dry.jsonl", three_records)
    stats = await ingest(ReplayAdapter(uri), adapter_row, dry_run=True)

    assert stats.fetched == 3
    assert stats.inserted == 0
    assert stats.dry_run is True
    assert _observations(adapter_row.adapter_id) == []


# ===========================================================================
# Idempotency
# ===========================================================================
async def test_replaying_the_same_export_twice_inserts_once(
    adapter_row: AdapterRow,
    tmp_path: Path,
    three_records: list[dict[str, Any]],
    write_jsonl: Any,
) -> None:
    uri = write_jsonl(tmp_path / "export.jsonl", three_records)

    first = await ingest(ReplayAdapter(uri), adapter_row)
    second = await ingest(ReplayAdapter(uri), adapter_row)

    assert first.inserted == 3
    assert second.inserted == 0
    assert second.duplicates == 3, "the dedup index must absorb them, not error"
    assert len(_observations(adapter_row.adapter_id)) == 3


async def test_two_adapters_seeing_the_same_record_keep_both_observations(
    seed: dict[str, Any],
    adapter_row: AdapterRow,
    tmp_path: Path,
    *,
    jira_mapping: dict[str, Any],
    three_records: list[dict[str, Any]],
    write_jsonl: Any,
) -> None:
    """adapter_id lives in the unique index, NOT in the dedup hash. The same
    event observed through two adapters is two independent measurements
    (db/014's comment says so explicitly), and collapsing them would
    under-count a business that is genuinely instrumented twice.
    """
    other_key = f"replay-{uuid.uuid4().hex[:8]}"
    other = registry.register_adapter(
        _dsn(),
        engagement_id=seed["engagement_id"],
        adapter_key=other_key,
        system_node_key="sys.jira",
        kind="rest_poll",
        mapping=jira_mapping,
    )
    other_row = AdapterRow(
        adapter_id=other["adapter_id"],
        engagement_id=seed["engagement_id"],
        adapter_key=other_key,
        system_node_key="sys.jira",
        kind="rest_poll",
        secret_arn=None,
        mapping=jira_mapping,
        last_cursor=None,
    )

    uri = write_jsonl(tmp_path / "export.jsonl", three_records)
    await ingest(ReplayAdapter(uri), adapter_row)
    await ingest(ReplayAdapter(uri), other_row)

    assert len(_observations(adapter_row.adapter_id)) == 3
    assert len(_observations(other_row.adapter_id)) == 3


# ===========================================================================
# Cursor advance, and crash-resume
# ===========================================================================
def _adapter_state(adapter_id: int) -> dict[str, Any]:
    rows = _query(
        "SELECT last_cursor, last_synced_at FROM sor.adapter WHERE adapter_id = %(aid)s",
        {"aid": adapter_id},
    )
    return rows[0]


async def test_cursor_and_last_synced_at_advance_with_the_batch(
    adapter_row: AdapterRow,
    tmp_path: Path,
    three_records: list[dict[str, Any]],
    write_jsonl: Any,
) -> None:
    before = _adapter_state(adapter_row.adapter_id)
    assert before["last_cursor"] is None
    assert before["last_synced_at"] is None

    uri = write_jsonl(tmp_path / "export.jsonl", three_records)
    stats = await ingest(ReplayAdapter(uri), adapter_row, batch_size=2)

    assert stats.batches == 2, "3 records at batch_size 2 is two transactions"
    after = _adapter_state(adapter_row.adapter_id)
    assert after["last_cursor"] == "2", "the last committed line number"
    assert after["last_synced_at"] is not None
    assert stats.last_cursor == "2"


class _FailAfter:
    """Yields from a list, then raises -- a source that dies mid-run."""

    kind = "replay"

    def __init__(self, records: list[dict[str, Any]], fail_at: int) -> None:
        self.records = records
        self.fail_at = fail_at

    async def fetch(self, cursor: str | None) -> Any:
        start = 0 if cursor is None else int(cursor) + 1
        for line_number, payload in enumerate(self.records):
            if line_number < start:
                continue
            if line_number == self.fail_at:
                msg = "simulated SoR failure mid-run"
                raise RuntimeError(msg)
            yield RawRecord(payload=payload, cursor=str(line_number), ref=str(line_number))


async def test_a_crash_mid_run_resumes_without_duplicating(
    adapter_row: AdapterRow,
    tmp_path: Path,
    make_record: Any,
    write_jsonl: Any,
) -> None:
    records = [
        make_record(f"CPQ-{i}", "In Review", f"2026-03-{i + 1:02d}T10:00:00Z") for i in range(6)
    ]

    # Dies after committing the first two batches (4 records).
    with pytest.raises(RuntimeError, match="simulated SoR failure"):
        await ingest(_FailAfter(records, fail_at=4), adapter_row, batch_size=2)

    committed = _adapter_state(adapter_row.adapter_id)
    assert committed["last_cursor"] == "3", "only committed batches advance the cursor"
    assert len(_observations(adapter_row.adapter_id)) == 4

    # A rerun picks up the cursor the crashed run left behind.
    resumed_row = AdapterRow(
        adapter_id=adapter_row.adapter_id,
        engagement_id=adapter_row.engagement_id,
        adapter_key=adapter_row.adapter_key,
        system_node_key=adapter_row.system_node_key,
        kind=adapter_row.kind,
        secret_arn=None,
        mapping=adapter_row.mapping,
        last_cursor=committed["last_cursor"],
    )
    uri = write_jsonl(tmp_path / "export.jsonl", records)
    stats = await ingest(ReplayAdapter(uri), resumed_row, batch_size=2)

    assert stats.inserted == 2, "only the records the crash never committed"
    assert stats.duplicates == 0
    rows = _observations(adapter_row.adapter_id)
    assert len(rows) == 6, "complete, and no duplicates"
    assert {r["case_ref"] for r in rows} == {f"CPQ-{i}" for i in range(6)}


async def test_a_rerun_from_scratch_after_a_crash_also_does_not_duplicate(
    adapter_row: AdapterRow,
    tmp_path: Path,
    make_record: Any,
    write_jsonl: Any,
) -> None:
    """The cursor makes resume efficient; the dedup index makes it CORRECT.
    Even an operator who reruns the whole export from line 0 gets one row per
    record.
    """
    records = [
        make_record(f"CPQ-{i}", "In Review", f"2026-03-{i + 1:02d}T10:00:00Z") for i in range(6)
    ]
    with pytest.raises(RuntimeError):
        await ingest(_FailAfter(records, fail_at=4), adapter_row, batch_size=2)

    uri = write_jsonl(tmp_path / "export.jsonl", records)
    stats = await ingest(ReplayAdapter(uri), adapter_row, batch_size=2)

    assert stats.inserted == 2
    assert stats.duplicates == 4
    assert len(_observations(adapter_row.adapter_id)) == 6


async def test_backfill_does_not_overwrite_the_live_watermark(
    adapter_row: AdapterRow,
    tmp_path: Path,
    three_records: list[dict[str, Any]],
    write_jsonl: Any,
) -> None:
    """A replay's cursor is a line number. Writing it over a rest_poll
    adapter's `updated >= ...` timestamp would make the next live poll ask the
    SoR for everything changed since "2".
    """
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE sor.adapter SET last_cursor = %(c)s WHERE adapter_id = %(aid)s",
            {"c": "2026-03-01T00:00:00Z", "aid": adapter_row.adapter_id},
        )

    uri = write_jsonl(tmp_path / "export.jsonl", three_records)
    stats = await backfill(
        engagement_id=adapter_row.engagement_id,
        adapter_key=adapter_row.adapter_key,
        s3_uri=uri,
    )

    assert stats.inserted == 3
    after = _adapter_state(adapter_row.adapter_id)
    assert after["last_cursor"] == "2026-03-01T00:00:00Z", (
        "the live source's watermark must survive a backfill"
    )
    assert after["last_synced_at"] is not None


# ===========================================================================
# The grant boundary
# ===========================================================================
async def test_fde_ingest_may_not_register_its_own_adapter(
    seed: dict[str, Any], jira_mapping: dict[str, Any]
) -> None:
    """db/014 lists this omission as a decision, not an oversight: the mapping
    IS the measurement instrument, so a runtime role that could register one
    could also define away the drift it is measured by.
    """
    with pytest.raises(pg_errors.InsufficientPrivilege):
        async with db.tool_transaction(role="fde_ingest") as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO sor.adapter
                    (engagement_id, adapter_key, system_node_key, kind, mapping)
                VALUES (%(eng)s::uuid, 'self-registered', 'sys.jira', 'rest_poll', %(mapping)s)
                """,
                {"eng": seed["engagement_id"], "mapping": Jsonb(jira_mapping)},
            )


async def test_fde_ingest_may_advance_its_own_cursor(adapter_row: AdapterRow) -> None:
    """The one write on sor.adapter the ingest role is granted (db/010:57) --
    asserted alongside the denial so the boundary reads as a shape, not a
    blanket refusal.
    """
    async with db.tool_transaction(role="fde_ingest") as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE sor.adapter SET last_cursor = 'probe' WHERE adapter_id = %(aid)s",
            {"aid": adapter_row.adapter_id},
        )
    assert _adapter_state(adapter_row.adapter_id)["last_cursor"] == "probe"
