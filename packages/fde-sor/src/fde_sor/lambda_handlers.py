"""lambda_handlers.py -- the four Lambda entry points.

One container image, four functions, distinguished only by `ImageConfig`:

    EntryPoint ["python", "-m", "awslambdaric"]
    Command    ["fde_sor.lambda_handlers.<handler>"]

| function | trigger | does |
|---|---|---|
| `fde-sor-poll`       | one EventBridge Scheduler schedule per adapter, generated from `sor.adapter.poll_cron` | rest_poll / db_cdc fetch through the shared pipeline |
| `fde-sor-stream`     | SQS event-source mapping (batch 10, ReportBatchItemFailures) | decode, ingest, return per-message failures |
| `fde-sor-backfill`   | AgentCore Gateway lambda target, or direct invoke | replay a JSONL export |
| `fde-sor-drift-scan` | EventBridge `rate(6 hours)` | `sor.run_all_detectors` per active engagement + SNS |

There is no expiry handler. Proposal expiry lives in the gate service, which
owns the `hitl` domain: putting it here would mean this package -- which holds
customer SoR credentials -- also held the `fde_gate_service` credential, the
one role that can merge into the graph.

Every handler is a thin `asyncio.run` around a coroutine that is itself
callable from the CLI, so nothing is only reachable through AWS and every path
below is exercised by a test that has never seen a Lambda.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fde_mcp.logging import configure_logging, get_logger
from fde_sor import backfill as backfill_module
from fde_sor import detectors, registry
from fde_sor.adapters.event_stream import StaticRecordAdapter, decode_sqs_records
from fde_sor.mapping import MappingSpec
from fde_sor.observations import ingest

__all__ = [
    "backfill_handler",
    "drift_scan_handler",
    "poll_handler",
    "stream_handler",
]

log = get_logger(__name__)


def _unwrap(event: dict[str, Any]) -> dict[str, Any]:
    """Find the tool arguments inside whatever wrapper invoked us.

    A direct `lambda:Invoke` passes the payload as the event. An AgentCore
    Gateway lambda target passes the tool input the same way, but a test
    harness or a console invocation habitually nests it under `input`,
    `arguments` or `body`. Accepting all four costs four lines and removes an
    entire category of "the Lambda ran and said engagement_id is required".
    """
    for key in ("arguments", "input", "body"):
        nested = event.get(key)
        if isinstance(nested, dict):
            return nested
    return event


def _require(args: dict[str, Any], *names: str) -> list[str]:
    return [name for name in names if not args.get(name)]


async def poll(engagement_id: str, adapter_key: str, *, dry_run: bool = False) -> dict[str, Any]:
    """One scheduled fetch for one adapter."""
    row = await registry.load_adapter(engagement_id, adapter_key)
    if not row.is_active:
        log.warning("poll_skipped_inactive_adapter", adapter_key=adapter_key)
        return {"adapter_key": adapter_key, "skipped": "adapter is not active"}
    adapter = registry.build_adapter(row)
    stats = await ingest(adapter, row, dry_run=dry_run)
    return stats.as_dict()


def poll_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    configure_logging()
    args = _unwrap(event)
    missing = _require(args, "engagement_id", "adapter_key")
    if missing:
        return {"error": f"missing required parameter(s): {', '.join(missing)}"}
    return asyncio.run(
        poll(
            str(args["engagement_id"]),
            str(args["adapter_key"]),
            dry_run=bool(args.get("dry_run", False)),
        )
    )


async def consume_sqs_batch(sqs_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Ingest one SQS batch and report which messages must be retried.

    Failure attribution is the whole point. Three distinct outcomes:

    * a message that will not decode -> that messageId fails, the rest land;
    * a message that decodes but will not normalise (a mapping mismatch, a
      missing case id) -> that messageId fails too, via `IngestStats.failed_refs`;
    * the insert itself fails -> EVERY messageId in the batch fails, because
      none of them are durable.

    Failing the whole batch on any single bad message would have SQS redeliver
    the nine good ones alongside it until the poison message aged out.
    """
    if not sqs_records:
        return {"batchItemFailures": []}

    source_arn = str(sqs_records[0].get("eventSourceARN", ""))
    row = await registry.load_adapter_by_queue(source_arn)
    spec = MappingSpec.parse(row.mapping)

    records, undecodable = decode_sqs_records(spec.stream, sqs_records)
    all_ids = [str(r.get("messageId", "")) for r in sqs_records]

    try:
        stats = await ingest(StaticRecordAdapter(records), row)
    except Exception:
        log.exception("sqs_batch_ingest_failed", adapter_key=row.adapter_key)
        return {"batchItemFailures": [{"itemIdentifier": mid} for mid in all_ids if mid]}

    failed = [*undecodable, *stats.failed_refs]
    return {
        "batchItemFailures": [{"itemIdentifier": mid} for mid in failed if mid],
        "stats": stats.as_dict(),
    }


def stream_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    configure_logging()
    records = event.get("Records", [])
    if not isinstance(records, list):
        return {"batchItemFailures": []}
    return asyncio.run(consume_sqs_batch([r for r in records if isinstance(r, dict)]))


def backfill_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """The `sor_backfill_observations` Gateway tool.

    Its accepted parameters are `backfill.BACKFILL_PARAMS`, which is also what
    `fde_agents/deploy/schemas/sor_backfill_tool_schema.json` declares and what
    `tests/test_gateway_schema.py` pins the two together on.
    """
    configure_logging()
    args = _unwrap(event)
    missing = _require(args, *backfill_module.BACKFILL_REQUIRED_PARAMS)
    if missing:
        return {"error": f"missing required parameter(s): {', '.join(missing)}"}
    unknown = sorted(set(args) - set(backfill_module.BACKFILL_PARAMS))
    if unknown:
        return {
            "error": f"unknown parameter(s): {', '.join(unknown)}",
            "hint": f"accepted parameters are {list(backfill_module.BACKFILL_PARAMS)}",
        }
    stats = asyncio.run(
        backfill_module.backfill(
            engagement_id=str(args["engagement_id"]),
            adapter_key=str(args["adapter_key"]),
            s3_uri=str(args["s3_uri"]),
            dry_run=bool(args.get("dry_run", False)),
        )
    )
    return stats.as_dict()


def drift_scan_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Scan one engagement if the event names one, else every active engagement."""
    configure_logging()
    args = _unwrap(event)
    engagement_id = args.get("engagement_id")
    if engagement_id:
        result = asyncio.run(detectors.drift_scan_once(str(engagement_id)))
        return {"scans": [result.as_dict()]}
    results = asyncio.run(detectors.scan_all_engagements())
    return {"scans": [r.as_dict() for r in results]}
