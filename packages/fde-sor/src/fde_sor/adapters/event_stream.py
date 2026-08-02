"""adapters/event_stream.py -- SQS-delivered events, two entry modes, one decoder.

Mode 1, serverless: an SQS event-source mapping invokes `fde-sor-stream` with
a batch of up to ten messages. `lambda_handlers.stream_handler` decodes them,
runs the shared pipeline, and returns `batchItemFailures` so a single poison
message is retried (and eventually DLQ'd) without redelivering the nine good
ones beside it.

Mode 2, containers: `fde-sor stream-consume --adapter-key X` long-polls the
queue itself, ingests, and deletes each message only AFTER its batch has
committed in Postgres. Deleting before the commit would drop events on a
crash; deleting after means at-least-once redelivery, which is exactly what
the `dedup_key` index (db/014) is for.

Both modes share `decode_sqs_records`, which is a pure function -- the
envelope handling below is the part that actually breaks in production
(everyone forgets SNS-over-SQS double-wraps the body), so it is unit-tested
with no queue involved.

`mapping.stream`:

    {"queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/jira-events",
     "envelope": "raw" | "sns",
     "record_path": "detail"}
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, ClassVar

from fde_mcp.logging import get_logger
from fde_sor.adapters.base import RawRecord
from fde_sor.config import aws_region, get_settings
from fde_sor.mapping import resolve_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = [
    "SqsPollingAdapter",
    "StaticRecordAdapter",
    "decode_sqs_body",
    "decode_sqs_records",
]

log = get_logger(__name__)


class EventStreamError(RuntimeError):
    """The stream configuration or a message envelope is unusable."""


def decode_sqs_body(body: str, stream_config: dict[str, Any]) -> dict[str, Any]:
    """Turn one SQS message body into the raw SoR record the mapping expects.

    `envelope: "sns"` unwraps the SNS notification SQS wraps around the real
    payload (the actual message is a JSON STRING under `Message`, so it needs
    a second `json.loads` -- this is the step that is forgotten roughly every
    time, and it fails as "every field is missing" rather than as a parse
    error, which is why it gets its own branch and its own test).

    `record_path` then reaches into the decoded body, for sources that nest
    the domain event under an envelope of their own (EventBridge's `detail`).
    """
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        msg = "SQS message body is not a JSON object"
        raise EventStreamError(msg)

    envelope = stream_config.get("envelope", "raw")
    if envelope == "sns":
        inner = parsed.get("Message")
        if inner is None:
            msg = "envelope is 'sns' but the body has no 'Message' field"
            raise EventStreamError(msg)
        parsed = json.loads(inner) if isinstance(inner, str) else inner
        if not isinstance(parsed, dict):
            msg = "SNS 'Message' did not contain a JSON object"
            raise EventStreamError(msg)
    elif envelope != "raw":
        msg = f"unknown envelope {envelope!r}; expected 'raw' or 'sns'"
        raise EventStreamError(msg)

    record_path = stream_config.get("record_path")
    if record_path:
        inner_record = resolve_path(parsed, str(record_path))
        if not isinstance(inner_record, dict):
            msg = f"record_path {record_path!r} did not resolve to an object"
            raise EventStreamError(msg)
        return inner_record
    return parsed


def decode_sqs_records(
    stream_config: dict[str, Any], sqs_records: list[dict[str, Any]]
) -> tuple[list[RawRecord], list[str]]:
    """Decode a Lambda SQS batch. Returns `(records, undecodable_message_ids)`.

    A message that cannot be decoded is NOT raised on: it is separated out and
    reported so the caller can fail exactly that message. Raising would fail
    the whole batch, and SQS would redeliver nine perfectly good messages
    alongside the broken one, forever.
    """
    records: list[RawRecord] = []
    failures: list[str] = []
    for sqs_record in sqs_records:
        message_id = str(sqs_record.get("messageId", ""))
        try:
            payload = decode_sqs_body(str(sqs_record.get("body", "")), stream_config)
        except (EventStreamError, json.JSONDecodeError) as exc:
            # message_id only, never the body: an SoR event routinely carries
            # the actor identifiers this platform hashes precisely so they
            # never reach a log.
            log.warning("sqs_message_undecodable", message_id=message_id, reason=str(exc))
            failures.append(message_id)
            continue
        records.append(RawRecord(payload=payload, cursor=message_id or None, ref=message_id))
    return records, failures


class StaticRecordAdapter:
    """Yields an already-decoded list of records.

    The Lambda path decodes its own batch (it has to, to attribute
    `batchItemFailures`) and then hands the result here, so that mode still
    runs through `observations.ingest` rather than growing a second copy of
    the mapping/hash/insert logic.
    """

    kind: ClassVar[str] = "event_stream"

    def __init__(self, records: list[RawRecord]) -> None:
        self._records = records

    async def fetch(self, cursor: str | None) -> AsyncIterator[RawRecord]:
        for record in self._records:
            yield record


class SqsPollingAdapter:
    """Long-polls an SQS queue; deletes messages only once they are committed.

    `on_batch_committed` (the `AckingAdapter` protocol) is what makes the
    ordering safe -- see the module docstring.
    """

    kind: ClassVar[str] = "event_stream"

    def __init__(
        self,
        stream_config: dict[str, Any],
        *,
        max_batches: int | None = None,
        client: Any = None,
    ) -> None:
        queue_url = stream_config.get("queue_url")
        if not queue_url:
            msg = "event_stream mapping needs a 'stream' object with a 'queue_url'"
            raise EventStreamError(msg)
        self.queue_url = str(queue_url)
        self.stream_config = stream_config
        #: `None` means "run until the queue is empty". The long-running
        #: Deployment passes nothing; `--once` passes 1.
        self.max_batches = max_batches
        self._client = client
        self._pending_receipts: list[str] = []

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # noqa: PLC0415 -- injectable for tests

            self._client = boto3.client("sqs", region_name=aws_region())
        return self._client

    async def fetch(self, cursor: str | None) -> AsyncIterator[RawRecord]:
        settings = get_settings()
        client = self._get_client()
        batches = 0
        while self.max_batches is None or batches < self.max_batches:
            response = await asyncio.to_thread(
                client.receive_message,
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=settings.sqs_batch_size,
                WaitTimeSeconds=settings.sqs_wait_seconds,
            )
            messages = response.get("Messages", [])
            batches += 1
            if not messages:
                return
            for message in messages:
                message_id = str(message.get("MessageId", ""))
                try:
                    payload = decode_sqs_body(str(message.get("Body", "")), self.stream_config)
                except (EventStreamError, json.JSONDecodeError) as exc:
                    # Left on the queue on purpose: its visibility timeout will
                    # expire, it will be redelivered, and the queue's own
                    # maxReceiveCount will move it to the DLQ. A malformed
                    # message is the queue owner's problem to look at, and
                    # deleting it here would destroy the evidence.
                    log.warning("sqs_message_undecodable", message_id=message_id, reason=str(exc))
                    continue
                self._pending_receipts.append(str(message["ReceiptHandle"]))
                yield RawRecord(payload=payload, cursor=message_id or None, ref=message_id)

    async def on_batch_committed(self, cursor: str | None) -> None:
        if not self._pending_receipts:
            return
        receipts, self._pending_receipts = self._pending_receipts, []
        client = self._get_client()
        entries = [{"Id": str(i), "ReceiptHandle": r} for i, r in enumerate(receipts)]
        # SQS caps DeleteMessageBatch at 10 entries per call.
        for start in range(0, len(entries), 10):
            await asyncio.to_thread(
                client.delete_message_batch,
                QueueUrl=self.queue_url,
                Entries=entries[start : start + 10],
            )
        log.info("sqs_messages_deleted", queue_url=self.queue_url, deleted=len(receipts))
