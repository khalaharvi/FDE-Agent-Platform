"""Tests for the event_stream adapter: envelope decoding and batch failures.

The decoder is pure, so the two things that actually break in production get
tested directly: SNS-over-SQS double-wrapping (which fails as "every mapped
field is missing", not as a parse error), and the `batchItemFailures`
contract, which is what stops one poison message from causing SQS to redeliver
the nine good messages beside it forever.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fde_sor.adapters.event_stream import (
    EventStreamError,
    SqsPollingAdapter,
    StaticRecordAdapter,
    decode_sqs_body,
    decode_sqs_records,
)

DOMAIN_EVENT: dict[str, Any] = {
    "quote_id": "Q-1",
    "event_type": "quote.legal.completed",
    "event_time": "2026-03-01T10:00:00Z",
}


def _sqs_record(message_id: str, body: Any) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "body": body if isinstance(body, str) else json.dumps(body),
        "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:fde-sor-jira-events",
        "receiptHandle": f"receipt-{message_id}",
    }


# ===========================================================================
# Envelope decoding
# ===========================================================================
def test_raw_envelope_is_the_body_itself() -> None:
    assert decode_sqs_body(json.dumps(DOMAIN_EVENT), {}) == DOMAIN_EVENT


def test_sns_envelope_unwraps_the_json_string_under_message() -> None:
    """SNS-over-SQS puts the real payload in `Message` as a STRING, so it needs
    a second json.loads. Forgetting that is the classic failure here.
    """
    body = {
        "Type": "Notification",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:quotes",
        "Message": json.dumps(DOMAIN_EVENT),
    }
    assert decode_sqs_body(json.dumps(body), {"envelope": "sns"}) == DOMAIN_EVENT


def test_sns_envelope_without_a_message_field_is_an_error() -> None:
    with pytest.raises(EventStreamError, match="Message"):
        decode_sqs_body(json.dumps({"Type": "Notification"}), {"envelope": "sns"})


def test_record_path_reaches_into_an_eventbridge_style_envelope() -> None:
    body = {"source": "acme.quotes", "detail-type": "quote", "detail": DOMAIN_EVENT}
    assert decode_sqs_body(json.dumps(body), {"record_path": "detail"}) == DOMAIN_EVENT


def test_record_path_and_sns_envelope_compose() -> None:
    inner = {"detail": DOMAIN_EVENT}
    body = {"Type": "Notification", "Message": json.dumps(inner)}
    decoded = decode_sqs_body(json.dumps(body), {"envelope": "sns", "record_path": "detail"})
    assert decoded == DOMAIN_EVENT


def test_an_unknown_envelope_is_an_error() -> None:
    with pytest.raises(EventStreamError, match="envelope"):
        decode_sqs_body(json.dumps(DOMAIN_EVENT), {"envelope": "protobuf"})


def test_a_body_that_is_not_an_object_is_an_error() -> None:
    with pytest.raises(EventStreamError):
        decode_sqs_body(json.dumps([1, 2, 3]), {})


# ===========================================================================
# Batch decoding and failure attribution
# ===========================================================================
def test_decode_separates_good_messages_from_undecodable_ones() -> None:
    records, failures = decode_sqs_records(
        {},
        [
            _sqs_record("m1", DOMAIN_EVENT),
            _sqs_record("m2", "{not json at all"),
            _sqs_record("m3", DOMAIN_EVENT),
        ],
    )
    assert [r.payload["quote_id"] for r in records] == ["Q-1", "Q-1"]
    assert [r.ref for r in records] == ["m1", "m3"]
    assert failures == ["m2"], (
        "only the poison message may fail; failing the batch would have SQS "
        "redeliver the good messages alongside it"
    )


def test_decode_carries_the_message_id_as_the_record_ref() -> None:
    """`ref` is how a normalisation failure deep in the shared pipeline gets
    attributed back to one SQS message.
    """
    records, _ = decode_sqs_records({}, [_sqs_record("m-42", DOMAIN_EVENT)])
    assert records[0].ref == "m-42"
    assert records[0].cursor == "m-42"


def test_an_undecodable_message_does_not_leak_its_body_into_the_failure_list() -> None:
    _, failures = decode_sqs_records(
        {}, [_sqs_record("m1", json.dumps({"actor": "jane.doe@customer.example.com"}) + "trailing")]
    )
    assert failures == ["m1"], "attribution is by message id; the body is never carried out"


async def test_static_record_adapter_replays_decoded_records() -> None:
    records, _ = decode_sqs_records({}, [_sqs_record("m1", DOMAIN_EVENT)])
    adapter = StaticRecordAdapter(records)
    collected = [r async for r in adapter.fetch(None)]
    assert [r.ref for r in collected] == ["m1"]


# ===========================================================================
# The long-poll consumer: delete only after commit
# ===========================================================================
class _FakeSqs:
    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self.batches = batches
        self.deleted: list[str] = []
        self.receives = 0

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        if self.receives >= len(self.batches):
            return {}
        batch = self.batches[self.receives]
        self.receives += 1
        return {"Messages": batch}

    def delete_message_batch(self, **kwargs: Any) -> dict[str, Any]:
        self.deleted.extend(e["ReceiptHandle"] for e in kwargs["Entries"])
        return {"Successful": kwargs["Entries"]}


def _sqs_message(message_id: str, body: Any) -> dict[str, Any]:
    return {
        "MessageId": message_id,
        "Body": json.dumps(body),
        "ReceiptHandle": f"receipt-{message_id}",
    }


async def test_polling_adapter_yields_messages_and_stops_when_the_queue_drains() -> None:
    client = _FakeSqs([[_sqs_message("m1", DOMAIN_EVENT), _sqs_message("m2", DOMAIN_EVENT)], []])
    adapter = SqsPollingAdapter({"queue_url": "https://sqs.invalid/q"}, client=client)
    records = [r async for r in adapter.fetch(None)]
    assert [r.ref for r in records] == ["m1", "m2"]


async def test_polling_adapter_deletes_nothing_until_a_batch_is_committed() -> None:
    """Deleting on receipt loses events on a crash. Deleting after the commit
    means at-least-once redelivery, which the dedup index absorbs.
    """
    client = _FakeSqs([[_sqs_message("m1", DOMAIN_EVENT)], []])
    adapter = SqsPollingAdapter({"queue_url": "https://sqs.invalid/q"}, client=client)
    _ = [r async for r in adapter.fetch(None)]
    assert client.deleted == [], "nothing may be deleted before on_batch_committed"

    await adapter.on_batch_committed("m1")
    assert client.deleted == ["receipt-m1"]


async def test_polling_adapter_leaves_an_undecodable_message_on_the_queue() -> None:
    """Not deleted, so its visibility timeout expires, it is redelivered, and
    the queue's own maxReceiveCount moves it to the DLQ where someone can look
    at it. Deleting it here would destroy the evidence.
    """
    client = _FakeSqs(
        [
            [
                _sqs_message("bad", DOMAIN_EVENT) | {"Body": "{broken"},
                _sqs_message("ok", DOMAIN_EVENT),
            ],
            [],
        ]
    )
    adapter = SqsPollingAdapter({"queue_url": "https://sqs.invalid/q"}, client=client)
    records = [r async for r in adapter.fetch(None)]
    assert [r.ref for r in records] == ["ok"]

    await adapter.on_batch_committed("ok")
    assert client.deleted == ["receipt-ok"], "only the message that landed is deleted"


async def test_polling_adapter_respects_max_batches() -> None:
    client = _FakeSqs([[_sqs_message("m1", DOMAIN_EVENT)], [_sqs_message("m2", DOMAIN_EVENT)]])
    adapter = SqsPollingAdapter(
        {"queue_url": "https://sqs.invalid/q"}, max_batches=1, client=client
    )
    records = [r async for r in adapter.fetch(None)]
    assert [r.ref for r in records] == ["m1"], "--once drains one receive batch and exits"


def test_a_stream_block_without_a_queue_url_is_refused() -> None:
    with pytest.raises(EventStreamError, match="queue_url"):
        SqsPollingAdapter({})
