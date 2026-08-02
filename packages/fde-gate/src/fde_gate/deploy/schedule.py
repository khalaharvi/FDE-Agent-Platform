"""Create the two EventBridge rules the gate service needs to be correct.

These are not housekeeping. Each one closes a specific hole:

* `fde-gate-tick`, every minute. Calls `wf.timeout_steps()` and then advances
  any run left with no open step. It is what makes the runner's
  two-transaction pattern safe -- a process that dies between `begin_step`
  and `complete_step` leaves a `running` attempt that nothing else would ever
  fail -- and it is the backstop for a lost async self-invoke. Without it, a
  single Lambda failure strands a run forever.

* `fde-gate-expiry`, hourly. Calls `hitl.expire_proposals()`. Without it,
  `expires_at` is a column nobody reads and the SLA in `hitl.gate_policy` is
  documentation rather than behaviour.

One minute is the floor for `rate()`, and it is the right floor: it bounds
how long a stranded step or a lost invoke can sit unnoticed, and the sweep
does nothing when there is nothing to do.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import boto3

from fde_mcp.logging import get_logger

log = get_logger(__name__)

DEFAULT_FUNCTION_NAME = "fde-gate-service"

# (rule name, schedule, event payload, description)
RULES: tuple[tuple[str, str, dict[str, str], str], ...] = (
    (
        "fde-gate-tick",
        "rate(1 minute)",
        {"source": "fde.gate.tick"},
        "Time out overdue workflow steps and advance idle runs",
    ),
    (
        "fde-gate-expiry",
        "rate(1 hour)",
        {"source": "fde.gate.expiry"},
        "Expire proposals past their gate SLA",
    ),
)


def create_rule(
    events: Any,
    lambda_client: Any,
    function_arn: str,
    function_name: str,
    rule: tuple[str, str, dict[str, str], str],
) -> str:
    name, schedule, payload, description = rule
    log.info("gate_schedule_creating", rule=name, schedule=schedule)
    response = events.put_rule(
        Name=name, ScheduleExpression=schedule, State="ENABLED", Description=description
    )
    rule_arn = str(response["RuleArn"])

    events.put_targets(
        Rule=name,
        Targets=[{"Id": f"{name}-target", "Arn": function_arn, "Input": json.dumps(payload)}],
    )

    # Per rule, not one blanket grant: a rule that is later deleted takes its
    # own permission with it, and nothing else in EventBridge inherits the
    # right to invoke a function that can merge into the graph.
    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=f"events-{name}",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
    except lambda_client.exceptions.ResourceConflictException:
        # Re-running after adding a rule should not fail on the permission
        # that is already exactly what we would have created.
        log.info("gate_schedule_permission_exists", rule=name)
    return rule_arn


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--function-name", default=DEFAULT_FUNCTION_NAME)
    p.add_argument("--region", dest="aws_region", default=os.environ.get("AWS_REGION"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    events = boto3.client("events", region_name=args.aws_region)
    lambda_client = boto3.client("lambda", region_name=args.aws_region)

    try:
        function_arn = lambda_client.get_function(FunctionName=args.function_name)["Configuration"][
            "FunctionArn"
        ]
        for rule in RULES:
            arn = create_rule(events, lambda_client, function_arn, args.function_name, rule)
            print(f"{rule[0]}: {arn}  ({rule[1]})")
    except Exception:
        log.exception("gate_schedule_deploy_failed", function_name=args.function_name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
