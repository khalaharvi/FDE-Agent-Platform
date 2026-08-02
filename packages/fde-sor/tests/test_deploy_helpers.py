"""Tests for the deploy module's pure parts.

The live AWS calls are not validated here (the repo's standing convention --
`fde_agents.deploy` says the same). What IS validated is everything that can
be wrong without AWS telling you: the cron translation, the per-function
`CreateFunction` arguments, and the schedule reconciliation's create/update/
delete decisions.

The cron translation earns its own tests because a wrong one is a SILENT
failure. EventBridge accepts the schedule, reports it healthy, and fires on
the wrong day -- or never. Nothing in a deployment surfaces that; the only
symptom is an adapter that quietly stops observing.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from fde_sor.deploy.sor_lambdas import (
    HANDLERS,
    DeployError,
    _to_scheduler_cron,
    build_function_kwargs,
    build_schedule_kwargs,
    print_iam_policies,
    schedule_name,
)


# ===========================================================================
# Cron translation
# ===========================================================================
@pytest.mark.parametrize(
    ("standard", "expected"),
    [
        # Both wildcards: day-of-week becomes '?', since EventBridge requires
        # exactly one of the two day fields to be '?'.
        ("*/15 * * * *", "cron(*/15 * * * ? *)"),
        ("0 */6 * * *", "cron(0 */6 * * ? *)"),
        ("0 3 * * *", "cron(0 3 * * ? *)"),
        # A specific day-of-month: day-of-week becomes '?'.
        ("0 0 1 * *", "cron(0 0 1 * ? *)"),
        ("30 2 15 6 *", "cron(30 2 15 6 ? *)"),
        # A specific day-of-week: day-of-month becomes '?', and the numbering
        # shifts (cron Sunday=0, EventBridge Sunday=1).
        ("0 9 * * 1", "cron(0 9 ? * 2 *)"),
        ("0 9 * * 0", "cron(0 9 ? * 1 *)"),
        ("0 9 * * 6", "cron(0 9 ? * 7 *)"),
        ("0 9 * * 1-5", "cron(0 9 ? * 2-6 *)"),
        ("0 9 * * 1,3,5", "cron(0 9 ? * 2,4,6 *)"),
        # Names mean the same thing in both dialects and pass through.
        ("0 9 * * MON", "cron(0 9 ? * MON *)"),
        ("0 9 * * MON-FRI", "cron(0 9 ? * MON-FRI *)"),
    ],
)
def test_cron_translation(standard: str, expected: str) -> None:
    assert _to_scheduler_cron(standard) == expected


def test_translation_always_produces_exactly_one_question_mark_day_field() -> None:
    """The invariant EventBridge actually enforces, checked directly rather
    than only via the table above.
    """
    for standard in ("*/5 * * * *", "0 0 1 * *", "0 9 * * 3"):
        fields = _to_scheduler_cron(standard).removeprefix("cron(").removesuffix(")").split()
        assert len(fields) == 6, "EventBridge cron has six fields, including year"
        day_of_month, day_of_week = fields[2], fields[4]
        assert (day_of_month == "?") != (day_of_week == "?"), (
            "exactly one of the day fields must be '?'"
        )


@pytest.mark.parametrize(
    "bad",
    [
        "@daily",
        "* * * *",  # four fields
        "0 0 * * * *",  # six fields -- already an EventBridge expression
        "",
    ],
)
def test_a_non_five_field_cron_is_refused(bad: str) -> None:
    with pytest.raises(DeployError, match="5-field"):
        _to_scheduler_cron(bad)


def test_constraining_both_day_fields_is_refused_rather_than_reinterpreted() -> None:
    """Standard cron ORs the two day fields; EventBridge has no equivalent.
    Silently picking one would change when a customer's adapter polls.
    """
    with pytest.raises(DeployError, match="day-of-month and day-of-week"):
        _to_scheduler_cron("0 9 1 * 1")


# ===========================================================================
# Function arguments
# ===========================================================================
def _lambda_args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "function_prefix": "fde-sor",
        "role_arn": "arn:aws:iam::123456789012:role/fde-sor-lambda",
        "image_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/fde-sor:v1",
        "aws_region": "us-east-1",
        "db_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:fde/db/ingest",
        "alert_topic_arn": None,
        "actor_salt_secret_prefix": None,
        "subnet_ids": ["subnet-a", "subnet-b"],
        "security_group_ids": ["sg-1"],
        "timeout_seconds": 900,
        "memory_mb": 1024,
        "update": False,
    }
    return argparse.Namespace(**{**defaults, **overrides})


def test_there_are_exactly_four_handlers_and_no_expiry_one() -> None:
    """Proposal expiry moved to the gate service, which owns the hitl domain.
    A fifth handler here would mean this package -- which holds customer SoR
    credentials -- also held the credential that can merge into the graph.
    """
    suffixes = {suffix for suffix, _ in HANDLERS.values()}
    assert suffixes == {"poll", "stream", "backfill", "drift-scan"}
    assert not any("expir" in h for h in HANDLERS)


def test_every_function_shares_one_image_and_differs_only_by_command() -> None:
    args = _lambda_args()
    built = [build_function_kwargs(handler, args) for handler in HANDLERS]

    assert {json.dumps(k["Code"]) for k in built} == {json.dumps({"ImageUri": args.image_uri})}
    assert {tuple(k["ImageConfig"]["EntryPoint"]) for k in built} == {
        ("python", "-m", "awslambdaric")
    }
    commands = [k["ImageConfig"]["Command"][0] for k in built]
    assert sorted(commands) == sorted(HANDLERS)
    assert len({k["FunctionName"] for k in built}) == 4


def test_functions_are_arm64() -> None:
    """Mandatory, and getting it wrong is silent: the image builds and runs
    under emulation on a laptop, then fails in the Lambda.
    """
    args = _lambda_args()
    for handler in HANDLERS:
        assert build_function_kwargs(handler, args)["Architectures"] == ["arm64"]


def test_vpc_config_is_attached_when_subnets_and_security_groups_are_given() -> None:
    kwargs = build_function_kwargs("fde_sor.lambda_handlers.poll_handler", _lambda_args())
    assert kwargs["VpcConfig"] == {
        "SubnetIds": ["subnet-a", "subnet-b"],
        "SecurityGroupIds": ["sg-1"],
    }


def test_vpc_config_is_omitted_when_either_half_is_missing() -> None:
    kwargs = build_function_kwargs(
        "fde_sor.lambda_handlers.poll_handler", _lambda_args(security_group_ids=None)
    )
    assert "VpcConfig" not in kwargs


def test_optional_environment_variables_are_omitted_when_unset() -> None:
    kwargs = build_function_kwargs("fde_sor.lambda_handlers.poll_handler", _lambda_args())
    env = kwargs["Environment"]["Variables"]
    assert env["FDE_DB_SECRET_ARN"].endswith("fde/db/ingest")
    assert "FDE_SOR_ALERT_TOPIC_ARN" not in env, (
        "an empty topic ARN would be published to and fail; absent means 'log instead'"
    )


# ===========================================================================
# Schedules
# ===========================================================================
def _schedule_args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "schedule_prefix": "fde-sor-poll",
        "poll_lambda_arn": "arn:aws:lambda:us-east-1:123456789012:function:fde-sor-poll",
        "scheduler_role_arn": "arn:aws:iam::123456789012:role/fde-sor-scheduler",
        "timezone": "UTC",
        "dry_run": False,
    }
    return argparse.Namespace(**{**defaults, **overrides})


def test_a_schedule_targets_the_poll_lambda_with_the_adapter_as_input() -> None:
    """One shared poll Lambda plus per-adapter input is what lets each
    customer keep the cadence they authored without a deployment per adapter.
    """
    engagement_id = "8b1d3f2a-0000-4000-8000-000000000001"
    kwargs = build_schedule_kwargs(_schedule_args(), engagement_id, "jira-prod", "*/15 * * * *")

    assert kwargs["ScheduleExpression"] == "cron(*/15 * * * ? *)"
    assert kwargs["Target"]["Arn"].endswith(":fde-sor-poll")
    assert json.loads(kwargs["Target"]["Input"]) == {
        "engagement_id": engagement_id,
        "adapter_key": "jira-prod",
    }
    assert kwargs["FlexibleTimeWindow"] == {"Mode": "OFF"}


def test_schedule_names_stay_inside_the_64_character_limit() -> None:
    name = schedule_name(
        "fde-sor-poll", "8b1d3f2a-0000-4000-8000-000000000001", "a-very-" + "long-" * 20 + "key"
    )
    assert len(name) <= 64


def test_schedule_names_contain_only_characters_eventbridge_accepts() -> None:
    name = schedule_name("fde-sor-poll", "8b1d3f2a-0000-4000-8000-000000000001", "jira/prod:eu")
    assert all(c.isalnum() or c in "-_." for c in name)


def test_schedule_names_are_stable_and_distinct_per_adapter() -> None:
    """Stability is what makes the sync a reconcile rather than a churn: the
    same adapter must map to the same schedule name on every run.
    """
    engagement_id = "8b1d3f2a-0000-4000-8000-000000000001"
    first = schedule_name("fde-sor-poll", engagement_id, "jira-prod")
    assert first == schedule_name("fde-sor-poll", engagement_id, "jira-prod")
    assert first != schedule_name("fde-sor-poll", engagement_id, "sfdc-cpq")


# ===========================================================================
# IAM
# ===========================================================================
def test_the_packaged_iam_documents_parse_and_carry_their_rationale() -> None:
    policies = print_iam_policies()
    assert policies["trust"]["Statement"][0]["Principal"]["Service"] == "lambda.amazonaws.com"
    assert "_comment" in policies["trust"], "the _comment convention explains WHY at the top"
    assert "_comment" in policies["permissions"]

    sids = {s["Sid"] for s in policies["permissions"]["Statement"]}
    assert {"ReadPlatformSecrets", "ConsumeAdapterQueues", "PublishDriftAlerts"} <= sids


def test_the_permissions_policy_grants_no_write_access_to_secrets() -> None:
    """This role reads a customer's SoR credential and the per-engagement
    actor-hash salts. It must never be able to change either.
    """
    actions: list[str] = []
    for statement in print_iam_policies()["permissions"]["Statement"]:
        action = statement["Action"]
        actions.extend([action] if isinstance(action, str) else action)
    secret_actions = [a for a in actions if a.startswith("secretsmanager:")]
    assert secret_actions == ["secretsmanager:GetSecretValue"]


def test_backfill_export_access_is_read_only() -> None:
    actions: list[str] = []
    for statement in print_iam_policies()["permissions"]["Statement"]:
        action = statement["Action"]
        actions.extend([action] if isinstance(action, str) else action)
    s3_actions = {a for a in actions if a.startswith("s3:")}
    assert s3_actions == {"s3:GetObject", "s3:ListBucket"}, (
        "a backfill that could write to the export bucket could rewrite the "
        "evidence it is about to ingest"
    )
