"""Provision the four fde-sor Lambdas, their schedules, and their SQS wiring.

One container image serves all four; they differ only in `ImageConfig.Command`,
which names the handler `awslambdaric` should call. That is why the deploy
runbook builds once and passes the same `--image-uri` four times.

Three subcommands, because they change on different cadences:

  * `lambdas`        -- once per release (a new image tag)
  * `sync-schedules` -- whenever an adapter's `poll_cron` changes, which is a
                        database edit, not a deployment
  * `wire-stream`    -- once per event_stream adapter, at onboarding

The dispatcher decision worth knowing: ONE poll Lambda with one EventBridge
Scheduler schedule per adapter, rather than one Lambda per adapter or a
self-scheduling tick. `poll_cron` is per-adapter in the database, so
per-adapter schedules keep each customer's cadence exactly as authored while
still deploying one piece of code; evaluating cron expressions in-process
instead would need a `croniter`-class dependency in the image that holds
customer SoR credentials, to reimplement something EventBridge does for free.

Live AWS calls are NOT validated here -- same convention as
`fde_agents.deploy.runtimes`. `_to_scheduler_cron` is, because a wrong cron
translation is a silent failure: the schedule is created, it just never fires
when you think it does.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from typing import Any

import boto3

from fde_mcp.logging import configure_logging, get_logger
from fde_sor import registry
from fde_sor.config import aws_region
from fde_sor.mapping import MappingSpec

log = get_logger(__name__)

#: handler -> (function name suffix, description). The image is identical.
HANDLERS: dict[str, tuple[str, str]] = {
    "fde_sor.lambda_handlers.poll_handler": (
        "poll",
        "Scheduled SoR poll (rest_poll/db_cdc) into sor.observation",
    ),
    "fde_sor.lambda_handlers.stream_handler": (
        "stream",
        "SQS event-source consumer into sor.observation",
    ),
    "fde_sor.lambda_handlers.backfill_handler": (
        "backfill",
        "Replay a JSONL export into sor.observation (AgentCore Gateway lambda target)",
    ),
    "fde_sor.lambda_handlers.drift_scan_handler": (
        "drift-scan",
        "Run sor.run_all_detectors per active engagement and escalate critical bypasses",
    ),
}

# 15 minutes is the Lambda maximum and is what a backfill of a large export
# needs; the others finish in seconds but share the image and the setting is
# a ceiling, not a reservation.
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MEMORY_MB = 1024
DRIFT_SCAN_SCHEDULE = "rate(6 hours)"

_DAY_OF_WEEK_SHIFT = {
    "0": "1",
    "1": "2",
    "2": "3",
    "3": "4",
    "4": "5",
    "5": "6",
    "6": "7",
    "7": "1",
}


class DeployError(RuntimeError):
    """A deployment input cannot be translated into an AWS call."""


def _shift_day_of_week(field: str) -> str:
    """Standard cron numbers Sunday 0..Saturday 6; EventBridge numbers it
    Sunday 1..Saturday 7. Names (MON, FRI) mean the same thing in both.

    Off-by-one here is the classic silent scheduling bug: the schedule is
    created successfully and fires on the wrong day, forever.
    """
    if field in ("*", "?"):
        return field
    out: list[str] = []
    token = ""
    for char in field:
        if char.isdigit():
            token += char
            continue
        if token:
            out.append(_DAY_OF_WEEK_SHIFT.get(token, token))
            token = ""
        out.append(char)
    if token:
        out.append(_DAY_OF_WEEK_SHIFT.get(token, token))
    return "".join(out)


def _to_scheduler_cron(poll_cron: str) -> str:
    """Translate a 5-field standard cron into an EventBridge `cron(...)`.

    EventBridge cron has SIX fields (minute hour day-of-month month
    day-of-week year) and requires EXACTLY ONE of day-of-month/day-of-week to
    be `?` -- a straight copy of a 5-field expression is rejected at
    create-schedule time, and a copy with `*` in both is rejected too. Both
    `*` (the overwhelmingly common case, "every day") becomes `* ... ?`.

    A cron specifying BOTH a day-of-month and a day-of-week is not
    expressible: EventBridge has no OR semantics for the two fields. That
    raises here, at deploy time, rather than being silently reinterpreted.
    """
    fields = poll_cron.split()
    if len(fields) != 5:
        msg = (
            f"poll_cron {poll_cron!r} is not a 5-field cron expression "
            "(minute hour day-of-month month day-of-week). Shorthands like "
            "@daily are not translated -- write them out."
        )
        raise DeployError(msg)

    minute, hour, day_of_month, month, day_of_week = fields
    dom_wild = day_of_month in ("*", "?")
    dow_wild = day_of_week in ("*", "?")

    if dom_wild and dow_wild:
        day_of_month, day_of_week = "*", "?"
    elif dom_wild:
        day_of_month, day_of_week = "?", _shift_day_of_week(day_of_week)
    elif dow_wild:
        day_of_week = "?"
    else:
        msg = (
            f"poll_cron {poll_cron!r} constrains both day-of-month and "
            "day-of-week; EventBridge Scheduler requires exactly one of them "
            "to be '?' and cannot express the union."
        )
        raise DeployError(msg)

    return f"cron({minute} {hour} {day_of_month} {month} {day_of_week} *)"


def _iam_document(name: str) -> dict[str, Any]:
    text = resources.files("fde_sor.deploy.iam").joinpath(name).read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def function_name(prefix: str, suffix: str) -> str:
    return f"{prefix}-{suffix}"


def _environment(args: argparse.Namespace) -> dict[str, str]:
    env = {"FDE_SERVICE_NAME": "fde-sor"}
    if args.db_secret_arn:
        env["FDE_DB_SECRET_ARN"] = args.db_secret_arn
    if args.aws_region:
        env["AWS_REGION"] = args.aws_region
    if args.alert_topic_arn:
        env["FDE_SOR_ALERT_TOPIC_ARN"] = args.alert_topic_arn
    if args.actor_salt_secret_prefix:
        env["FDE_ACTOR_SALT_SECRET_PREFIX"] = args.actor_salt_secret_prefix
    return env


def build_function_kwargs(handler: str, args: argparse.Namespace) -> dict[str, Any]:
    """Assemble one `CreateFunction` call. Pure, so it is unit-testable."""
    suffix, description = HANDLERS[handler]
    kwargs: dict[str, Any] = {
        "FunctionName": function_name(args.function_prefix, suffix),
        "Role": args.role_arn,
        "Code": {"ImageUri": args.image_uri},
        "PackageType": "Image",
        "ImageConfig": {
            "EntryPoint": ["python", "-m", "awslambdaric"],
            "Command": [handler],
        },
        "Description": description,
        "Timeout": args.timeout_seconds,
        "MemorySize": args.memory_mb,
        "Architectures": ["arm64"],
        "Environment": {"Variables": _environment(args)},
    }
    if args.subnet_ids and args.security_group_ids:
        # The database is not public; without a VPC config every one of these
        # functions fails at connect time, which looks like a credentials
        # problem and is not.
        kwargs["VpcConfig"] = {
            "SubnetIds": args.subnet_ids,
            "SecurityGroupIds": args.security_group_ids,
        }
    return kwargs


def deploy_lambdas(client: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    for handler in HANDLERS:
        kwargs = build_function_kwargs(handler, args)
        name = kwargs["FunctionName"]
        if args.update:
            response = client.update_function_code(
                FunctionName=name, ImageUri=args.image_uri, Architectures=["arm64"]
            )
            log.info("sor_lambda_updated", function_name=name)
        else:
            response = client.create_function(**kwargs)
            log.info("sor_lambda_created", function_name=name, handler=handler)
        created.append({"FunctionName": name, "FunctionArn": response.get("FunctionArn")})
    return created


def schedule_name(prefix: str, engagement_id: str, adapter_key: str) -> str:
    """EventBridge Scheduler names allow [0-9a-zA-Z-_.] and cap at 64 chars.

    The engagement uuid's first segment plus the adapter key is unique in
    practice and stays inside the cap; the full uuid plus a long adapter key
    would not.
    """
    short_engagement = engagement_id.split("-", maxsplit=1)[0]
    safe_key = "".join(c if c.isalnum() or c in "-_." else "-" for c in adapter_key)
    return f"{prefix}-{short_engagement}-{safe_key}"[:64]


def build_schedule_kwargs(
    args: argparse.Namespace, engagement_id: str, adapter_key: str, poll_cron: str
) -> dict[str, Any]:
    """Assemble one `CreateSchedule` call. Pure, so it is unit-testable."""
    return {
        "Name": schedule_name(args.schedule_prefix, engagement_id, adapter_key),
        "ScheduleExpression": _to_scheduler_cron(poll_cron),
        "ScheduleExpressionTimezone": args.timezone,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": args.poll_lambda_arn,
            "RoleArn": args.scheduler_role_arn,
            "Input": json.dumps({"engagement_id": engagement_id, "adapter_key": adapter_key}),
        },
        "Description": f"fde-sor poll for {adapter_key} ({engagement_id})",
        "State": "ENABLED",
    }


async def sync_schedules(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Reconcile EventBridge Scheduler against `sor.adapter`.

    Create for a newly polled adapter, update when `poll_cron` changed, delete
    when the adapter was disabled or its cron cleared. Reconciling rather than
    creating means a `poll_cron` edit in the database is picked up by re-running
    one command, and there is no second place where a schedule is authored.
    """
    adapters = await registry.list_adapters(active_only=False)
    desired = {
        schedule_name(args.schedule_prefix, a.engagement_id, a.adapter_key): a
        for a in adapters
        if a.is_active and a.poll_cron and a.kind != "event_stream"
    }

    existing: set[str] = set()
    paginator = client.get_paginator("list_schedules")
    for page in paginator.paginate(NamePrefix=args.schedule_prefix):
        existing.update(s["Name"] for s in page.get("Schedules", []))

    created, updated, deleted = [], [], []
    for name, adapter in desired.items():
        assert adapter.poll_cron is not None  # guarded by the comprehension above
        kwargs = build_schedule_kwargs(
            args, adapter.engagement_id, adapter.adapter_key, adapter.poll_cron
        )
        if name in existing:
            if not args.dry_run:
                client.update_schedule(**kwargs)
            updated.append(name)
        else:
            if not args.dry_run:
                client.create_schedule(**kwargs)
            created.append(name)

    for name in sorted(existing - set(desired)):
        if not args.dry_run:
            client.delete_schedule(Name=name)
        deleted.append(name)

    log.info(
        "sor_schedules_synced",
        created=len(created),
        updated=len(updated),
        deleted=len(deleted),
        dry_run=args.dry_run,
    )
    return {"created": created, "updated": updated, "deleted": deleted, "dry_run": args.dry_run}


async def wire_stream(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Attach an event_stream adapter's SQS queue to the stream Lambda.

    `FunctionResponseTypes=['ReportBatchItemFailures']` is what makes
    `stream_handler`'s `batchItemFailures` return value mean anything -- without
    it Lambda ignores the response and redelivers the entire batch on any
    failure, which is the behaviour that handler exists to avoid.
    """
    row = await registry.load_adapter(args.engagement_id, args.adapter_key)
    if row.kind != "event_stream":
        msg = f"adapter {args.adapter_key!r} is kind {row.kind!r}, not event_stream"
        raise DeployError(msg)
    queue_url = MappingSpec.parse(row.mapping).stream.get("queue_url")
    if not queue_url:
        msg = f"adapter {args.adapter_key!r} has no mapping.stream.queue_url"
        raise DeployError(msg)

    sqs = boto3.client("sqs", region_name=args.aws_region)
    attributes = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    queue_arn = attributes["Attributes"]["QueueArn"]

    response = client.create_event_source_mapping(
        EventSourceArn=queue_arn,
        FunctionName=args.stream_lambda_name,
        BatchSize=args.batch_size,
        FunctionResponseTypes=["ReportBatchItemFailures"],
        Enabled=True,
    )
    log.info(
        "sor_stream_wired",
        adapter_key=args.adapter_key,
        queue_arn=queue_arn,
        uuid=response.get("UUID"),
    )
    return {"adapter_key": args.adapter_key, "queue_arn": queue_arn, "uuid": response.get("UUID")}


def print_iam_policies() -> dict[str, Any]:
    """Emit the two packaged IAM documents, for a deploy pipeline to render."""
    return {
        "trust": _iam_document("sor-lambda-trust-policy.json"),
        "permissions": _iam_document("sor-lambda-permissions-policy.json"),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="fde-sor deploy", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def _common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--region", dest="aws_region", default=aws_region())
        sp.add_argument("--function-prefix", default="fde-sor")

    lambdas = sub.add_parser("lambdas", help="create/update the four Lambda functions")
    _common(lambdas)
    lambdas.add_argument("--image-uri", required=True, help="ECR image URI (arm64)")
    lambdas.add_argument(
        "--role-arn", required=True, help="Lambda execution role (see deploy/iam/)"
    )
    lambdas.add_argument("--db-secret-arn", default=None)
    lambdas.add_argument("--alert-topic-arn", default=None)
    lambdas.add_argument("--actor-salt-secret-prefix", default=None)
    lambdas.add_argument("--subnet-ids", nargs="*", default=None)
    lambdas.add_argument("--security-group-ids", nargs="*", default=None)
    lambdas.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    lambdas.add_argument("--memory-mb", type=int, default=DEFAULT_MEMORY_MB)
    lambdas.add_argument("--update", action="store_true", help="update code instead of creating")

    schedules = sub.add_parser(
        "sync-schedules", help="reconcile EventBridge Scheduler against sor.adapter.poll_cron"
    )
    _common(schedules)
    schedules.add_argument("--poll-lambda-arn", required=True)
    schedules.add_argument("--scheduler-role-arn", required=True)
    schedules.add_argument("--schedule-prefix", default="fde-sor-poll")
    schedules.add_argument("--timezone", default="UTC")
    schedules.add_argument("--dry-run", action="store_true")

    stream = sub.add_parser(
        "wire-stream", help="attach an adapter's SQS queue to the stream Lambda"
    )
    _common(stream)
    stream.add_argument("--engagement-id", required=True)
    stream.add_argument("--adapter-key", required=True)
    stream.add_argument("--stream-lambda-name", default="fde-sor-stream")
    stream.add_argument("--batch-size", type=int, default=10)

    sub.add_parser("iam", help="print the packaged trust and permissions policy documents")

    return p.parse_args(argv)


def run(argv: list[str] | None = None) -> tuple[int, Any]:
    """Execute one deploy subcommand and return `(exit_code, payload)`.

    Separated from `main` so the module does the work and the caller does the
    printing -- `fde_sor.cli` is the CLI surface and owns stdout, and a deploy
    step that returned its result instead of only printing it is also the one
    a test can assert on.
    """
    import asyncio  # noqa: PLC0415 -- only the async subcommands need a loop

    configure_logging()
    args = _parse_args(argv)

    if args.command == "iam":
        return 0, print_iam_policies()

    if args.command == "lambdas":
        client = boto3.client("lambda", region_name=args.aws_region)
        return 0, deploy_lambdas(client, args)

    if args.command == "sync-schedules":
        client = boto3.client("scheduler", region_name=args.aws_region)
        return 0, asyncio.run(sync_schedules(client, args))

    if args.command == "wire-stream":
        client = boto3.client("lambda", region_name=args.aws_region)
        return 0, asyncio.run(wire_stream(client, args))

    log.error("unknown_deploy_command", command=args.command)
    return 2, None


def main(argv: list[str] | None = None) -> int:
    code, payload = run(argv)
    if payload is not None:
        print(json.dumps(payload, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
