"""Create or update the gate service Lambda function.

Named `lambda_fn` rather than `lambda` for the obvious reason, and the
subcommand is still `fde-gate-deploy lambda`.

Create and update are separate flags, not one "upsert". `create` fails on a
name collision, which is the right outcome for a function that holds the
platform's merge authority: a script that silently reconfigures an existing
function is a script that can silently repoint it at a different database
secret or a different execution role.

Configuration worth knowing
----------------------------
* `Timeout=900` (the maximum) because an async self-invoke executes a whole
  agent step. Synchronous API requests never approach it -- they hand slow
  steps off and return 202 (see `runner`).
* `Architectures=['arm64']` must match what `package.py` built for. They are
  set from the same default in both files for a reason: a mismatch is an
  ImportError on the first request, not a build failure.
* `FDE_GATE_FUNCTION_NAME` is set to the function's own name. That is what
  enables the async self-invoke at all; without it every step runs inline and
  slow ones will hit API Gateway's 29-second wall.
* `ReservedConcurrentExecutions` defaults to unset. The runner's mutex is
  `wf.begin_step`'s row lock, not Lambda concurrency, so throttling here
  would only add latency to a correctness property the database already
  holds.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import boto3

from fde_mcp.logging import get_logger

log = get_logger(__name__)

DEFAULT_FUNCTION_NAME = "fde-gate-service"
DEFAULT_HANDLER = "fde_gate.handler.lambda_handler"
DEFAULT_RUNTIME = "python3.12"
DEFAULT_ARCHITECTURE = "arm64"
DEFAULT_TIMEOUT_S = 900
DEFAULT_MEMORY_MB = 512


def _environment(args: argparse.Namespace) -> dict[str, str]:
    env: dict[str, str] = {
        # Enables the async self-invoke; see the module docstring.
        "FDE_GATE_FUNCTION_NAME": args.function_name,
        "FDE_SERVICE_NAME": "fde-gate",
    }
    if args.db_secret_arn:
        env["FDE_DB_SECRET_ARN"] = args.db_secret_arn
    if args.db_dsn:
        env["FDE_DB_DSN"] = args.db_dsn
    if args.mcp_url:
        env["FDE_MCP_URL"] = args.mcp_url
    for pair in args.env or []:
        key, _, value = pair.partition("=")
        if not key or not _:
            msg = f"--env expects KEY=VALUE, got {pair!r}"
            raise ValueError(msg)
        env[key] = value
    return env


def _code(args: argparse.Namespace) -> dict[str, Any]:
    if args.s3_bucket:
        return {"S3Bucket": args.s3_bucket, "S3Key": args.s3_key or args.zip.name}
    return {"ZipFile": Path(args.zip).read_bytes()}


def create_function(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "FunctionName": args.function_name,
        "Runtime": DEFAULT_RUNTIME,
        "Role": args.role_arn,
        "Handler": DEFAULT_HANDLER,
        "Code": _code(args),
        "Timeout": args.timeout_s,
        "MemorySize": args.memory_mb,
        "Architectures": [args.architecture],
        "Environment": {"Variables": _environment(args)},
        "Description": "FDE gate service: review queue, merge, workflow runner, console",
        "Publish": True,
    }
    if args.subnet_ids and args.security_group_ids:
        # The database is normally not public. Both lists are required
        # together -- one without the other is an API error, and catching it
        # here names which half is missing.
        kwargs["VpcConfig"] = {
            "SubnetIds": args.subnet_ids,
            "SecurityGroupIds": args.security_group_ids,
        }

    log.info("gate_lambda_creating", function_name=args.function_name)
    response = client.create_function(**kwargs)
    log.info(
        "gate_lambda_created",
        function_name=args.function_name,
        function_arn=response.get("FunctionArn"),
        version=response.get("Version"),
    )
    return response  # type: ignore[no-any-return]


def update_function(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Update code first, then configuration.

    That order matters when the two change together: new configuration
    pointing at code that has not landed yet would be live for the seconds
    between the calls.
    """
    code = _code(args)
    log.info("gate_lambda_updating_code", function_name=args.function_name)
    client.update_function_code(FunctionName=args.function_name, Publish=True, **code)

    waiter = client.get_waiter("function_updated_v2")
    waiter.wait(FunctionName=args.function_name)

    log.info("gate_lambda_updating_config", function_name=args.function_name)
    response = client.update_function_configuration(
        FunctionName=args.function_name,
        Role=args.role_arn,
        Handler=DEFAULT_HANDLER,
        Timeout=args.timeout_s,
        MemorySize=args.memory_mb,
        Environment={"Variables": _environment(args)},
    )
    log.info("gate_lambda_updated", function_name=args.function_name)
    return response  # type: ignore[no-any-return]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--function-name", default=DEFAULT_FUNCTION_NAME)
    p.add_argument("--role-arn", required=True, help="execution role ARN (see deploy/iam/)")
    p.add_argument("--zip", type=Path, default=Path("dist/fde-gate.zip"))
    p.add_argument("--s3-bucket", default=None, help="upload from S3 instead of inline zip")
    p.add_argument("--s3-key", default=None)
    p.add_argument("--region", dest="aws_region", default=os.environ.get("AWS_REGION"))
    p.add_argument("--db-secret-arn", default=os.environ.get("FDE_DB_SECRET_ARN"))
    p.add_argument("--db-dsn", default=None, help="only for non-production environments")
    p.add_argument("--mcp-url", default=os.environ.get("FDE_MCP_URL"))
    p.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--memory-mb", type=int, default=DEFAULT_MEMORY_MB)
    p.add_argument("--architecture", default=DEFAULT_ARCHITECTURE, choices=["arm64", "x86_64"])
    p.add_argument("--subnet-ids", nargs="*", default=None)
    p.add_argument("--security-group-ids", nargs="*", default=None)
    p.add_argument(
        "--env",
        nargs="*",
        default=None,
        metavar="KEY=VALUE",
        help="extra environment variables, e.g. FDE_RUNTIME_ARN_WORKFLOW=arn:...",
    )
    p.add_argument(
        "--update",
        action="store_true",
        help="update the existing function instead of creating one",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.s3_bucket and not Path(args.zip).is_file():
        print(
            f"{args.zip} does not exist; run `fde-gate-deploy package` first",
            file=sys.stderr,
        )
        return 2

    client = boto3.client("lambda", region_name=args.aws_region)
    try:
        response = update_function(client, args) if args.update else create_function(client, args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        log.exception("gate_lambda_deploy_failed", function_name=args.function_name)
        return 1

    print(f"{args.function_name}: {response.get('FunctionArn')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
