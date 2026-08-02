"""The loop this whole package exists to close, end to end.

A graph that asserts "discount approval is gated by the discount-threshold
control", a system of record that shows the control being skipped, and a scan
that notices. Until `sor.observation` had a writer, `sor.run_all_detectors`
ran against an empty table and correctly found nothing -- the detectors in
db/007 have always worked; there was simply never anything to detect.

This test builds its OWN engagement rather than reusing the session `seed`.
`detect_control_bypass` aggregates over every observation in an engagement, so
sharing one with the pipeline tests would make its case counts depend on which
tests ran first -- and a drift test whose sample size is incidental is a drift
test that can pass for the wrong reason.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fde_sor import detectors, registry
from fde_sor.adapters.base import AdapterRow
from fde_sor.adapters.replay import ReplayAdapter
from fde_sor.observations import ingest

pytestmark = pytest.mark.requires_db

# detect_control_bypass's p_min_samples default: a smaller sample is discarded
# as noise (db/007:240), so a test with 9 cases would pass by finding nothing.
MIN_SAMPLES = 10
BYPASSED_CASES = 11
COMPLIANT_CASES = 2


def _dsn() -> str:
    return os.environ["FDE_DB_DSN"]


@pytest.fixture
def drift_engagement(jira_mapping: dict[str, Any]) -> dict[str, Any]:
    """An engagement whose graph says discount approval is gated by a control."""
    engagement_id = str(uuid.uuid4())
    nodes = [
        ("act.discount_approval", "activity", "Discount Approval", {}),
        ("ctrl.discount_threshold", "control", "Discount Threshold Control", {}),
        ("sys.jira", "system", "Jira", {}),
    ]
    with (
        psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO kg.commit (engagement_id, status, title, authored_by, sealed_by, sealed_at) "
            "VALUES (%(eng)s, 'sealed', 'drift fixture', 'pytest', 'pytest', now()) "
            "RETURNING commit_id",
            {"eng": engagement_id},
        )
        commit = cur.fetchone()
        assert commit is not None
        for key, node_type, label, attributes in nodes:
            cur.execute(
                "INSERT INTO kg.node "
                "(engagement_id, node_key, node_type, label, attributes, commit_id) "
                "VALUES (%(eng)s, %(key)s, %(t)s, %(l)s, %(a)s, %(c)s)",
                {
                    "eng": engagement_id,
                    "key": key,
                    "t": node_type,
                    "l": label,
                    "a": Jsonb(attributes),
                    "c": commit["commit_id"],
                },
            )
        cur.execute(
            """
            INSERT INTO kg.edge
                (engagement_id, edge_key, src_key, dst_key, edge_type, commit_id, human_confirmed)
            VALUES (%(eng)s, 'gate', 'act.discount_approval', 'ctrl.discount_threshold',
                    'gated_by', %(c)s, true)
            """,
            {"eng": engagement_id, "c": commit["commit_id"]},
        )

    record = registry.register_adapter(
        _dsn(),
        engagement_id=engagement_id,
        adapter_key="jira-drift",
        system_node_key="sys.jira",
        kind="rest_poll",
        mapping=jira_mapping,
    )
    return {
        "engagement_id": engagement_id,
        "adapter_row": AdapterRow(
            adapter_id=record["adapter_id"],
            engagement_id=engagement_id,
            adapter_key="jira-drift",
            system_node_key="sys.jira",
            kind="rest_poll",
            secret_arn=None,
            mapping=jira_mapping,
            last_cursor=None,
        ),
    }


@pytest.fixture
def bypass_export(tmp_path: Path, make_record: Any, write_jsonl: Any) -> str:
    """Cases that skipped the control, plus a couple that did not.

    Timestamps are relative to now because the detector's lookback is
    `now() - 90 days`; a checked-in export with fixed dates would age out and
    start passing by detecting nothing.
    """
    now = datetime.now(UTC)
    records: list[dict[str, Any]] = []
    for i in range(BYPASSED_CASES):
        when = (now - timedelta(days=i + 1)).isoformat()
        records.append(make_record(f"BYPASS-{i}", "Deal Desk Approved", when))
    for i in range(COMPLIANT_CASES):
        checked = (now - timedelta(days=i + 1, hours=2)).isoformat()
        approved = (now - timedelta(days=i + 1)).isoformat()
        records.append(make_record(f"OK-{i}", "Threshold Checked", checked))
        records.append(make_record(f"OK-{i}", "Deal Desk Approved", approved))
    return write_jsonl(tmp_path / "bypass.jsonl", records)


def _signals(engagement_id: str) -> list[dict[str, Any]]:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM sor.drift_signal WHERE engagement_id = %(eng)s::uuid "
            "AND drift_kind = 'control_bypass'",
            {"eng": engagement_id},
        )
        return list(cur.fetchall())


async def test_replayed_observations_raise_a_control_bypass_signal(
    drift_engagement: dict[str, Any], bypass_export: str
) -> None:
    engagement_id = drift_engagement["engagement_id"]

    # Before ingest there is nothing to detect -- asserted so the test cannot
    # pass on a signal something else left behind.
    empty = await detectors.drift_scan_once(engagement_id)
    assert empty.summary["control_bypass"] == 0
    assert _signals(engagement_id) == []

    stats = await ingest(ReplayAdapter(bypass_export), drift_engagement["adapter_row"])
    assert stats.inserted == BYPASSED_CASES + COMPLIANT_CASES * 2

    result = await detectors.drift_scan_once(engagement_id)
    assert result.summary["control_bypass"] == 1, (
        "the graph says this activity is gated and the SoR shows it was not"
    )

    signals = _signals(engagement_id)
    assert len(signals) == 1
    signal = signals[0]
    assert signal["subject_ref"] == "ctrl.discount_threshold"
    assert signal["severity"] == "critical", (
        f"{BYPASSED_CASES}/{BYPASSED_CASES + COMPLIANT_CASES} bypassed is well "
        "over the 10% critical threshold"
    )
    assert signal["state"] == "open"
    assert signal["sample_size"] == BYPASSED_CASES + COMPLIANT_CASES
    assert signal["detail"]["bypassed_cases"] == BYPASSED_CASES
    assert signal["detail"]["gated_activity"] == "act.discount_approval"


async def test_rescanning_dedups_into_occurrences_rather_than_a_second_signal(
    drift_engagement: dict[str, Any], bypass_export: str
) -> None:
    """docs/08 section 6: a noisy queue is worse than none. The same drift
    re-detected must bump `occurrences` and `last_seen_at`, not add a row.
    """
    engagement_id = drift_engagement["engagement_id"]
    await ingest(ReplayAdapter(bypass_export), drift_engagement["adapter_row"])

    await detectors.drift_scan_once(engagement_id)
    first = _signals(engagement_id)[0]

    await detectors.drift_scan_once(engagement_id)
    signals = _signals(engagement_id)

    assert len(signals) == 1, "one open signal per (kind, subject), not one per scan"
    assert signals[0]["occurrences"] == 2
    assert signals[0]["signal_id"] == first["signal_id"]
    assert signals[0]["last_seen_at"] >= first["last_seen_at"]


async def test_the_critical_bypass_is_returned_for_escalation(
    drift_engagement: dict[str, Any], bypass_export: str
) -> None:
    """The scan reads its own signals back -- which needs db/014's
    `GRANT SELECT ON sor.drift_signal TO fde_ingest`. Without it the detector
    could write signals the scanner could not see, and nothing would ever page.
    """
    engagement_id = drift_engagement["engagement_id"]
    await ingest(ReplayAdapter(bypass_export), drift_engagement["adapter_row"])

    result = await detectors.drift_scan_once(engagement_id)

    assert len(result.critical_bypasses) == 1
    assert result.critical_bypasses[0]["subject_ref"] == "ctrl.discount_threshold"
    assert result.alert_published is False, "no FDE_SOR_ALERT_TOPIC_ARN is configured in tests"


async def test_a_rescan_with_no_new_drift_does_not_re_escalate(
    drift_engagement: dict[str, Any], bypass_export: str
) -> None:
    """`last_seen_at >= scan start` is what stops an unresolved open signal
    paging someone every six hours forever. The second scan still REFRESHES the
    signal, so this asserts the escalation window, not the dedup.
    """
    engagement_id = drift_engagement["engagement_id"]
    await ingest(ReplayAdapter(bypass_export), drift_engagement["adapter_row"])
    await detectors.drift_scan_once(engagement_id)

    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        # Simulate an operator having triaged it: a signal that is no longer
        # 'open' must not be escalated again.
        cur.execute(
            "UPDATE sor.drift_signal SET state = 'triaged' "
            "WHERE engagement_id = %(eng)s::uuid AND drift_kind = 'control_bypass'",
            {"eng": engagement_id},
        )

    result = await detectors.drift_scan_once(engagement_id)
    assert result.critical_bypasses == [], "a triaged signal is not re-escalated"


async def test_scan_all_engagements_covers_the_one_with_an_active_adapter(
    drift_engagement: dict[str, Any], bypass_export: str
) -> None:
    await ingest(ReplayAdapter(bypass_export), drift_engagement["adapter_row"])

    results = await detectors.scan_all_engagements()
    scanned = {r.engagement_id for r in results}
    assert drift_engagement["engagement_id"] in scanned

    mine = next(r for r in results if r.engagement_id == drift_engagement["engagement_id"])
    assert mine.summary["control_bypass"] == 1
