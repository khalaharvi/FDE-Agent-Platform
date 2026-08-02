"""cli.py -- `fde-sor`, the operator and container entry point.

Every mode the Lambdas offer is also a subcommand here, because the EKS run
target is the same image driven by `command:` in a CronJob or Deployment
(infra/k8s/). Two entry points, one implementation, so nothing is only
reachable through AWS.

    fde-sor adapter register --engagement-id ... --adapter-key jira-prod \
        --system-node-key sys.jira --kind rest_poll --mapping-file jira.json \
        --poll-cron '*/15 * * * *' --dsn postgresql:///fde
    fde-sor adapter list [--engagement-id ...]
    fde-sor adapter disable --engagement-id ... --adapter-key jira-prod --dsn ...
    fde-sor adapter create-slot --engagement-id ... --adapter-key pg-cdc --dsn ...

    fde-sor poll --engagement-id ... --adapter-key jira-prod [--dry-run]
    fde-sor stream-consume --engagement-id ... --adapter-key jira-events [--once]
    fde-sor replay --engagement-id ... --adapter-key jira-prod --uri export.jsonl
    fde-sor backfill --engagement-id ... --adapter-key jira-prod --s3-uri s3://...
    fde-sor drift-scan [--engagement-id ...] [--all-engagements]
    fde-sor deploy {lambdas,sync-schedules,wire-stream,iam} ...

The registration commands take `--dsn` (or `FDE_SOR_OPERATOR_DSN`) and connect
as the operator, NOT as `fde_ingest` -- see `registry`'s module docstring for
why writing `sor.adapter` is deliberately outside every runtime role's reach.
The ingest commands take no DSN: they go through `fde_mcp.db`'s pool and
`SET LOCAL ROLE`, like everything else that touches the platform database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from fde_mcp import db
from fde_mcp.logging import configure_logging, get_logger
from fde_sor import backfill as backfill_module
from fde_sor import detectors, lambda_handlers, registry
from fde_sor.adapters.db_cdc import DEFAULT_PLUGIN, create_replication_slot
from fde_sor.adapters.event_stream import SqsPollingAdapter
from fde_sor.adapters.replay import ReplayAdapter
from fde_sor.mapping import MappingSpec
from fde_sor.observations import ingest

log = get_logger(__name__)

__all__ = ["main"]


def _operator_dsn(args: argparse.Namespace) -> str:
    dsn = args.dsn or os.environ.get("FDE_SOR_OPERATOR_DSN") or os.environ.get("FDE_DB_DSN")
    if not dsn:
        msg = (
            "adapter registration needs an operator DSN: pass --dsn, or set "
            "FDE_SOR_OPERATOR_DSN. This is deliberately not the fde_ingest "
            "connection -- that role has no INSERT on sor.adapter."
        )
        raise SystemExit(msg)
    return str(dsn)


def _load_mapping(args: argparse.Namespace) -> dict[str, Any]:
    if args.mapping_file:
        raw = Path(args.mapping_file).read_text(encoding="utf-8")
    elif args.mapping:
        raw = args.mapping
    else:
        raise SystemExit("pass --mapping-file or --mapping (inline JSON)")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise SystemExit("mapping must be a JSON object")
    return parsed


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


# ---------------------------------------------------------------------------
# Async command bodies
# ---------------------------------------------------------------------------
async def _cmd_adapter_list(args: argparse.Namespace) -> int:
    rows = await registry.list_adapters(engagement_id=args.engagement_id, active_only=not args.all)
    _emit(
        [
            {
                "adapter_id": r.adapter_id,
                "engagement_id": r.engagement_id,
                "adapter_key": r.adapter_key,
                "kind": r.kind,
                "system_node_key": r.system_node_key,
                "poll_cron": r.poll_cron,
                "is_active": r.is_active,
                "last_cursor": r.last_cursor,
            }
            for r in rows
        ]
    )
    return 0


async def _cmd_poll(args: argparse.Namespace) -> int:
    stats = await lambda_handlers.poll(args.engagement_id, args.adapter_key, dry_run=args.dry_run)
    _emit(stats)
    return 0


async def _cmd_stream_consume(args: argparse.Namespace) -> int:
    row = await registry.load_adapter(args.engagement_id, args.adapter_key)
    spec = MappingSpec.parse(row.mapping)
    # `--once` drains what is on the queue and exits (a CronJob); without it
    # the loop runs until the queue is empty and the caller restarts it (a
    # Deployment with restartPolicy Always).
    adapter = SqsPollingAdapter(spec.stream, max_batches=1 if args.once else None)
    stats = await ingest(adapter, row)
    _emit(stats.as_dict())
    return 0


async def _cmd_replay(args: argparse.Namespace) -> int:
    row = await registry.load_adapter(args.engagement_id, args.adapter_key)
    stats = await ingest(ReplayAdapter(args.uri), row, dry_run=args.dry_run, advance_cursor=False)
    _emit(stats.as_dict())
    return 0


async def _cmd_backfill(args: argparse.Namespace) -> int:
    stats = await backfill_module.backfill(
        engagement_id=args.engagement_id,
        adapter_key=args.adapter_key,
        s3_uri=args.s3_uri,
        dry_run=args.dry_run,
    )
    _emit(stats.as_dict())
    return 0


async def _cmd_drift_scan(args: argparse.Namespace) -> int:
    if args.engagement_id:
        result = await detectors.drift_scan_once(args.engagement_id)
        _emit(result.as_dict())
        return 0
    results = await detectors.scan_all_engagements()
    _emit([r.as_dict() for r in results])
    return 0


async def _cmd_create_slot(args: argparse.Namespace) -> int:
    row = await registry.load_adapter(args.engagement_id, args.adapter_key)
    cdc = MappingSpec.parse(row.mapping).cdc
    slot_name = cdc.get("slot_name")
    if not slot_name:
        raise SystemExit(f"adapter {args.adapter_key!r} has no mapping.cdc.slot_name")
    lsn = create_replication_slot(
        _operator_dsn(args), str(slot_name), str(cdc.get("plugin", DEFAULT_PLUGIN))
    )
    _emit({"slot_name": slot_name, "lsn": lsn, "created": lsn is not None})
    return 0


_ASYNC_COMMANDS = {
    "adapter:list": _cmd_adapter_list,
    "adapter:create-slot": _cmd_create_slot,
    "poll": _cmd_poll,
    "stream-consume": _cmd_stream_consume,
    "replay": _cmd_replay,
    "backfill": _cmd_backfill,
    "drift-scan": _cmd_drift_scan,
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fde-sor", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    def _adapter_target(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--engagement-id", required=True)
        sp.add_argument("--adapter-key", required=True)

    # -- adapter ------------------------------------------------------------
    adapter = sub.add_parser("adapter", help="register and inspect sor.adapter rows")
    adapter_sub = adapter.add_subparsers(dest="subcommand", required=True)

    register = adapter_sub.add_parser("register", help="register an adapter (operator DSN)")
    _adapter_target(register)
    register.add_argument("--system-node-key", required=True)
    register.add_argument("--kind", required=True, choices=registry.ADAPTER_KINDS)
    register.add_argument("--mapping-file")
    register.add_argument("--mapping", help="inline JSON mapping")
    register.add_argument("--secret-arn", default=None)
    register.add_argument("--poll-cron", default=None)
    register.add_argument("--dsn", default=None, help="operator DSN (or FDE_SOR_OPERATOR_DSN)")
    register.add_argument(
        "--create-slot",
        action="store_true",
        help="for db_cdc: also create the logical replication slot on the customer DB",
    )
    register.add_argument(
        "--customer-dsn", default=None, help="customer DSN used only by --create-slot"
    )

    listing = adapter_sub.add_parser("list", help="list registered adapters")
    listing.add_argument("--engagement-id", default=None)
    listing.add_argument("--all", action="store_true", help="include inactive adapters")

    disable = adapter_sub.add_parser("disable", help="set is_active = false (operator DSN)")
    _adapter_target(disable)
    disable.add_argument("--dsn", default=None)

    slot = adapter_sub.add_parser(
        "create-slot", help="create a db_cdc adapter's replication slot on the customer DB"
    )
    _adapter_target(slot)
    slot.add_argument("--dsn", required=True, help="the CUSTOMER database DSN")

    # -- ingest -------------------------------------------------------------
    poll = sub.add_parser("poll", help="run one scheduled fetch for an adapter")
    _adapter_target(poll)
    poll.add_argument("--dry-run", action="store_true")
    poll.add_argument(
        "--once", action="store_true", help="accepted for symmetry; a poll is one run"
    )

    stream = sub.add_parser("stream-consume", help="long-poll an event_stream adapter's SQS queue")
    _adapter_target(stream)
    stream.add_argument("--once", action="store_true", help="drain one receive batch and exit")

    replay = sub.add_parser("replay", help="replay a JSONL file or s3:// export")
    _adapter_target(replay)
    replay.add_argument("--uri", required=True)
    replay.add_argument("--dry-run", action="store_true")

    backfill = sub.add_parser("backfill", help="the fde-sor-backfill Lambda body, run locally")
    _adapter_target(backfill)
    backfill.add_argument("--s3-uri", required=True)
    backfill.add_argument("--dry-run", action="store_true")

    scan = sub.add_parser("drift-scan", help="run all detectors and escalate critical bypasses")
    scan.add_argument("--engagement-id", default=None)
    scan.add_argument(
        "--all-engagements",
        action="store_true",
        help="explicit form of the default (every engagement with an active adapter)",
    )

    # -- deploy -------------------------------------------------------------
    sub.add_parser(
        "deploy",
        help="provision Lambdas/schedules/SQS wiring (see `fde-sor deploy --help`)",
        add_help=False,
    )
    return p


def _run_sync_command(args: argparse.Namespace) -> int | None:
    """The commands that must NOT go through `fde_mcp.db` -- see registry."""
    if args.command != "adapter":
        return None
    if args.subcommand == "register":
        mapping = _load_mapping(args)
        record = registry.register_adapter(
            _operator_dsn(args),
            engagement_id=args.engagement_id,
            adapter_key=args.adapter_key,
            system_node_key=args.system_node_key,
            kind=args.kind,
            mapping=mapping,
            secret_arn=args.secret_arn,
            poll_cron=args.poll_cron,
        )
        if args.create_slot:
            if args.kind != "db_cdc":
                raise SystemExit("--create-slot only applies to --kind db_cdc")
            if not args.customer_dsn:
                raise SystemExit("--create-slot needs --customer-dsn (the CUSTOMER database)")
            cdc = MappingSpec.parse(mapping).cdc
            record["slot_lsn"] = create_replication_slot(
                args.customer_dsn,
                str(cdc["slot_name"]),
                str(cdc.get("plugin", DEFAULT_PLUGIN)),
            )
        _emit(record)
        return 0
    if args.subcommand == "disable":
        disabled = registry.disable_adapter(
            _operator_dsn(args),
            engagement_id=args.engagement_id,
            adapter_key=args.adapter_key,
        )
        if disabled is None:
            raise SystemExit(f"no adapter {args.adapter_key!r} for engagement {args.engagement_id}")
        _emit({k: v for k, v in disabled.items() if k != "mapping"})
        return 0
    return None


def _expected_errors() -> tuple[type[Exception], ...]:
    """The failure modes an operator can cause and fix -- misconfiguration,
    an unregistered adapter, a bad mapping, a missing salt. These print as
    one-line errors; anything else is a bug and keeps its traceback.
    Imported lazily so `--help` stays fast.
    """
    from fde_sor.adapters.event_stream import EventStreamError  # noqa: PLC0415
    from fde_sor.adapters.rest_poll import RestPollError  # noqa: PLC0415
    from fde_sor.hashing import SaltUnavailableError  # noqa: PLC0415
    from fde_sor.mapping import MappingError, RecordError  # noqa: PLC0415
    from fde_sor.registry import AdapterNotFoundError  # noqa: PLC0415

    return (
        db.ConfigError,
        AdapterNotFoundError,
        MappingError,
        RecordError,
        SaltUnavailableError,
        RestPollError,
        EventStreamError,
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    # `deploy` has its own argparse tree (deploy/sor_lambdas.py) and its own
    # subcommands; handing the rest of the line straight over keeps the two
    # from having to agree on flags.
    if raw_argv and raw_argv[0] == "deploy":
        from fde_sor.deploy import sor_lambdas  # noqa: PLC0415 -- boto3 import cost is deploy-only

        return sor_lambdas.main(raw_argv[1:])

    args = _build_parser().parse_args(raw_argv)

    try:
        sync_result = _run_sync_command(args)
        if sync_result is not None:
            return sync_result

        key = f"{args.command}:{args.subcommand}" if args.command == "adapter" else args.command
        handler = _ASYNC_COMMANDS.get(key)
        if handler is None:
            log.error("unknown_command", command=key)
            return 2

        async def _run() -> int:
            try:
                return await handler(args)
            finally:
                await db.close_pool()

        return asyncio.run(_run())
    except _expected_errors() as exc:
        sys.stderr.write(f"fde-sor: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
