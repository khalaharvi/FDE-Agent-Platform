"""Tests for the four Lambda entry points.

The SQS handler gets the most attention because its return value is a
CONTRACT, not a convenience: with `FunctionResponseTypes=['ReportBatchItemFailures']`
on the event-source mapping, whatever `batchItemFailures` contains is exactly
what SQS redelivers. Get it wrong in one direction and good messages are
reprocessed forever; wrong in the other and a poison message is silently
acknowledged and lost.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from fde_sor import lambda_handlers, registry

pytestmark = pytest.mark.requires_db


def _dsn() -> str:
    return os.environ["FDE_DB_DSN"]


@pytest.fixture
def stream_adapter(seed: dict[str, Any], jira_mapping: dict[str, Any]) -> dict[str, Any]:
    """One event_stream adapter, on a queue name unique to this test.

    The queue is how one `fde-sor-stream` Lambda serves every event_stream
    adapter: the event-source mapping reports which queue delivered the batch,
    and the queue identifies the adapter. The alternative -- an
    `FDE_SOR_ADAPTER_KEY` env var -- would need one Lambda per customer
    integration.

    The queue name is per-test because `load_adapter_by_queue` REFUSES an
    ambiguous match, and two adapters sharing a queue is exactly that. (It
    caught this fixture sharing one, which is the behaviour working.)
    """
    suffix = uuid.uuid4().hex[:8]
    queue_name = f"fde-sor-jira-events-{suffix}"
    mapping = {
        **jira_mapping,
        "stream": {
            "queue_url": f"https://sqs.us-east-1.amazonaws.com/123456789012/{queue_name}",
            "envelope": "raw",
        },
    }
    record = registry.register_adapter(
        _dsn(),
        engagement_id=seed["engagement_id"],
        adapter_key=f"stream-{suffix}",
        system_node_key="sys.jira",
        kind="event_stream",
        mapping=mapping,
    )
    return {
        "adapter_key": f"stream-{suffix}",
        "adapter_id": record["adapter_id"],
        "queue_arn": f"arn:aws:sqs:us-east-1:123456789012:{queue_name}",
    }


def _sqs_event(queue_arn: str, records: list[tuple[str, Any]]) -> dict[str, Any]:
    return {
        "Records": [
            {
                "messageId": message_id,
                "body": body if isinstance(body, str) else json.dumps(body),
                "eventSourceARN": queue_arn,
                "receiptHandle": f"receipt-{message_id}",
            }
            for message_id, body in records
        ]
    }


def _observation_count(adapter_id: int) -> int:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM sor.observation WHERE adapter_id = %(aid)s",
            {"aid": adapter_id},
        )
        row = cur.fetchone()
    return int(row["n"]) if row else 0


# ===========================================================================
# stream_handler
# ===========================================================================
async def test_a_clean_batch_lands_and_reports_no_failures(
    stream_adapter: dict[str, Any], make_record: Any
) -> None:
    event = _sqs_event(
        stream_adapter["queue_arn"],
        [
            ("m1", make_record("CPQ-1", "In Review", "2026-03-01T10:00:00Z")),
            ("m2", make_record("CPQ-2", "In Review", "2026-03-02T10:00:00Z")),
        ],
    )
    result = await lambda_handlers.consume_sqs_batch(event["Records"])

    assert result["batchItemFailures"] == []
    assert result["stats"]["inserted"] == 2
    assert _observation_count(stream_adapter["adapter_id"]) == 2


async def test_an_undecodable_message_fails_alone(
    stream_adapter: dict[str, Any], make_record: Any
) -> None:
    event = _sqs_event(
        stream_adapter["queue_arn"],
        [
            ("m1", make_record("CPQ-1", "In Review", "2026-03-01T10:00:00Z")),
            ("m2", "{ not json at all"),
            ("m3", make_record("CPQ-3", "In Review", "2026-03-03T10:00:00Z")),
        ],
    )
    result = await lambda_handlers.consume_sqs_batch(event["Records"])

    assert result["batchItemFailures"] == [{"itemIdentifier": "m2"}]
    assert _observation_count(stream_adapter["adapter_id"]) == 2, (
        "the two good messages must land; failing the batch would redeliver them"
    )


async def test_a_message_that_decodes_but_will_not_normalise_also_fails_alone(
    stream_adapter: dict[str, Any], make_record: Any
) -> None:
    """A mapping mismatch (here: no timestamp field) is a poison message too.
    Retrying will not fix it, so it fails, is redelivered, and the queue's own
    maxReceiveCount moves it to the DLQ -- which is the queue owner's problem
    to look at, not something this platform should swallow.
    """
    event = _sqs_event(
        stream_adapter["queue_arn"],
        [
            ("m1", make_record("CPQ-1", "In Review", "2026-03-01T10:00:00Z")),
            ("m2", {"key": "CPQ-2", "fields": {"status": {"name": "In Review"}}}),
        ],
    )
    result = await lambda_handlers.consume_sqs_batch(event["Records"])

    assert result["batchItemFailures"] == [{"itemIdentifier": "m2"}]
    assert result["stats"]["inserted"] == 1
    assert result["stats"]["record_errors"], "the reason is reported, naming the field"


async def test_redelivering_the_same_message_does_not_double_count(
    stream_adapter: dict[str, Any], make_record: Any
) -> None:
    """SQS delivery is at-least-once BY CONTRACT, so this is the normal case,
    not an edge case. db/014's dedup index is what absorbs it.
    """
    event = _sqs_event(
        stream_adapter["queue_arn"],
        [("m1", make_record("CPQ-1", "In Review", "2026-03-01T10:00:00Z"))],
    )

    first = await lambda_handlers.consume_sqs_batch(event["Records"])
    # A redelivery arrives with a NEW messageId but the same body.
    second = await lambda_handlers.consume_sqs_batch(
        _sqs_event(
            stream_adapter["queue_arn"],
            [("m1-redelivered", make_record("CPQ-1", "In Review", "2026-03-01T10:00:00Z"))],
        )["Records"]
    )

    assert first["stats"]["inserted"] == 1
    assert second["stats"]["inserted"] == 0
    assert second["stats"]["duplicates"] == 1
    assert second["batchItemFailures"] == [], "a duplicate is absorbed, not failed"
    assert _observation_count(stream_adapter["adapter_id"]) == 1


async def test_an_unknown_queue_is_an_error_not_a_silent_drop(
    stream_adapter: dict[str, Any], make_record: Any
) -> None:
    records = _sqs_event(
        stream_adapter["queue_arn"],
        [("m1", make_record("CPQ-1", "In Review", "2026-03-01T10:00:00Z"))],
    )["Records"]
    records[0]["eventSourceARN"] = "arn:aws:sqs:us-east-1:123456789012:not-registered"
    with pytest.raises(registry.AdapterNotFoundError):
        await lambda_handlers.consume_sqs_batch(records)


def test_an_empty_batch_is_a_no_op() -> None:
    assert lambda_handlers.stream_handler({"Records": []}) == {"batchItemFailures": []}


# ===========================================================================
# poll_handler / drift_scan_handler argument handling
# ===========================================================================
def test_poll_handler_reports_missing_parameters() -> None:
    result = lambda_handlers.poll_handler({"engagement_id": "x"})
    assert "adapter_key" in result["error"]


@pytest.mark.parametrize("wrapper", ["arguments", "input", "body"])
def test_handlers_unwrap_nested_invocation_payloads(wrapper: str) -> None:
    """A direct Invoke passes the payload flat; consoles and test harnesses
    habitually nest it. Accepting both removes a whole category of "the Lambda
    ran and said engagement_id is required".
    """
    result = lambda_handlers.poll_handler({wrapper: {"engagement_id": "x"}})
    assert "adapter_key" in result["error"]
    assert "engagement_id" not in result["error"]


async def test_poll_skips_an_inactive_adapter(
    seed: dict[str, Any], jira_mapping: dict[str, Any]
) -> None:
    key = f"disabled-{uuid.uuid4().hex[:8]}"
    registry.register_adapter(
        _dsn(),
        engagement_id=seed["engagement_id"],
        adapter_key=key,
        system_node_key="sys.jira",
        kind="rest_poll",
        mapping=jira_mapping,
    )
    registry.disable_adapter(_dsn(), engagement_id=seed["engagement_id"], adapter_key=key)

    result = await lambda_handlers.poll(seed["engagement_id"], key)
    assert "skipped" in result, "a disabled adapter must not call the customer's system"
