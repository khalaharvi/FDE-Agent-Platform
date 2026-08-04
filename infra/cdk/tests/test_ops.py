from __future__ import annotations

import json

from tests.test_synth import synth_template

# name -> (namespace, metric name, threshold, evaluation periods, period
# seconds, statistic) -- the brief's fixed alarm inventory, pinned exactly.
_EXPECTED_ALARMS: dict[str, tuple[str, str, float, int, int, str]] = {
    "FdeGateErrors": ("AWS/Lambda", "Errors", 3, 3, 60, "Sum"),
    "FdeMigrationRunnerErrors": ("AWS/Lambda", "Errors", 1, 1, 300, "Sum"),
    "FdeMcpUnhealthyHosts": ("AWS/ApplicationELB", "UnHealthyHostCount", 1, 3, 60, "Average"),
    "FdeMcpTarget5xx": ("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", 5, 1, 300, "Sum"),
    "FdeDbAcuCeiling": ("AWS/RDS", "ServerlessDatabaseCapacity", 7.5, 1, 900, "Average"),
    "FdeDbConnections": ("AWS/RDS", "DatabaseConnections", 360, 1, 300, "Average"),
    "FdeEmbedQueueBacklog": ("FDE/Platform", "EmbedQueueDepth", 500, 1, 900, "Average"),
    "FdeOpsDlqMessages": ("AWS/SQS", "ApproximateNumberOfMessagesVisible", 1, 1, 300, "Maximum"),
}


def _alarms_by_name(t) -> dict[str, dict]:
    alarms = t.find_resources("AWS::CloudWatch::Alarm")
    return {v["Properties"]["AlarmName"]: v for v in alarms.values()}


# ---------------------------------------------------------------------
# The seam: SNS topic
# ---------------------------------------------------------------------


def test_topic_exists_named_and_conditioned() -> None:
    t = synth_template()
    topics = t.find_resources("AWS::SNS::Topic")
    assert len(topics) == 1
    topic = next(iter(topics.values()))
    assert topic["Properties"]["TopicName"] == "fde-ops"
    assert topic["Condition"] == "OpsEnabled"


def test_email_subscription_conditioned_on_ops_email_mode() -> None:
    t = synth_template()
    subs = t.find_resources("AWS::SNS::Subscription")
    assert len(subs) == 1
    sub = next(iter(subs.values()))
    assert sub["Properties"]["Protocol"] == "email"
    assert sub["Condition"] == "OpsEmailEnabled"
    # Address resolves to OpsAlertEmail if set, else AdminEmail.
    assert sub["Properties"]["Endpoint"] == {
        "Fn::If": ["HasOpsAlertEmail", {"Ref": "OpsAlertEmail"}, {"Ref": "AdminEmail"}]
    }


# ---------------------------------------------------------------------
# Eight alarms
# ---------------------------------------------------------------------


def test_exactly_eight_alarms_exist() -> None:
    t = synth_template()
    assert len(t.find_resources("AWS::CloudWatch::Alarm")) == 8
    assert set(_alarms_by_name(t)) == set(_EXPECTED_ALARMS)


def test_every_alarm_shape_is_pinned() -> None:
    t = synth_template()
    by_name = _alarms_by_name(t)
    for name, (
        namespace,
        metric_name,
        threshold,
        eval_periods,
        period,
        statistic,
    ) in _EXPECTED_ALARMS.items():
        props = by_name[name]["Properties"]
        assert props["Namespace"] == namespace, name
        assert props["MetricName"] == metric_name, name
        assert props["Threshold"] == threshold, name
        assert props["EvaluationPeriods"] == eval_periods, name
        assert props["Period"] == period, name
        assert props["Statistic"] == statistic, name
        assert props["ComparisonOperator"] == "GreaterThanOrEqualToThreshold", name


def test_every_alarm_targets_the_topic() -> None:
    t = synth_template()
    topics = t.find_resources("AWS::SNS::Topic")
    topic_id = next(iter(topics))
    for name, alarm in _alarms_by_name(t).items():
        assert alarm["Properties"]["AlarmActions"] == [{"Ref": topic_id}], name


def test_every_alarm_carries_ops_enabled_condition() -> None:
    t = synth_template()
    for name, alarm in _alarms_by_name(t).items():
        assert alarm["Condition"] == "OpsEnabled", name


# ---------------------------------------------------------------------
# Embed-queue metrics feed: EventBridge -> migration Lambda
# ---------------------------------------------------------------------


def test_ops_metrics_rule_exists_conditioned_and_targets_migration_function() -> None:
    t = synth_template()
    template = t.to_json()
    migration_fn_id = next(
        k
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::Lambda::Function"
        and v["Properties"].get("Handler") == "handler.handler"
    )
    rules = t.find_resources("AWS::Events::Rule")
    metrics_rules = [
        r for r in rules.values() if r["Properties"]["ScheduleExpression"] == "rate(5 minutes)"
    ]
    assert len(metrics_rules) == 1
    rule = metrics_rules[0]
    assert rule["Condition"] == "OpsEnabled"
    target = rule["Properties"]["Targets"][0]
    assert target["Arn"] == {"Fn::GetAtt": [migration_fn_id, "Arn"]}
    assert target["Input"] == '{"source":"fde.ops.metrics"}'


def test_ops_metrics_rule_permission_is_also_conditioned() -> None:
    """The subtle part of "every OpsLayer resource carries OpsEnabled":
    `add_target`'s auto-generated `AWS::Lambda::Permission` lands as a
    child of the RULE (verified independently -- see ops.py's own module
    docstring), not the function, and CDK does not condition it for free.
    Exactly one of the three EventBridge-invoke permissions (two
    pre-existing gate schedules, unconditional, plus this one) must carry
    `Condition: OpsEnabled`."""
    t = synth_template()
    perms = t.find_resources("AWS::Lambda::Permission")
    event_perms = [
        p for p in perms.values() if p["Properties"].get("Principal") == "events.amazonaws.com"
    ]
    assert len(event_perms) == 3
    conditioned = [p for p in event_perms if p.get("Condition") == "OpsEnabled"]
    unconditioned = [p for p in event_perms if "Condition" not in p]
    assert len(conditioned) == 1
    assert len(unconditioned) == 2


# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------


def test_dashboard_exists_named_and_conditioned() -> None:
    t = synth_template()
    dashboards = t.find_resources("AWS::CloudWatch::Dashboard")
    assert len(dashboards) == 1
    dashboard = next(iter(dashboards.values()))
    assert dashboard["Properties"]["DashboardName"] == "FdeOps"
    assert dashboard["Condition"] == "OpsEnabled"


# ---------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------


def test_budget_conditioned_on_ops_enabled_and_nonzero_budget() -> None:
    t = synth_template()
    budgets = t.find_resources("AWS::Budgets::Budget")
    assert len(budgets) == 1
    budget = next(iter(budgets.values()))
    assert budget["Condition"] == "OpsBudgetEnabled"
    data = budget["Properties"]["Budget"]
    assert data["BudgetType"] == "COST"
    assert data["TimeUnit"] == "MONTHLY"
    assert data["BudgetLimit"] == {"Amount": {"Ref": "MonthlyBudgetUsd"}, "Unit": "USD"}
    notification = budget["Properties"]["NotificationsWithSubscribers"][0]
    assert notification["Notification"]["ComparisonOperator"] == "GREATER_THAN"
    assert notification["Notification"]["Threshold"] == 80
    subscriber = notification["Subscribers"][0]
    assert subscriber["SubscriptionType"] == "EMAIL"
    assert subscriber["Address"] == {
        "Fn::If": ["HasOpsAlertEmail", {"Ref": "OpsAlertEmail"}, {"Ref": "AdminEmail"}]
    }


def test_ops_budget_enabled_condition_is_and_of_ops_enabled_and_nonzero() -> None:
    template = synth_template().to_json()
    assert template["Conditions"]["OpsBudgetEnabled"] == {
        "Fn::And": [
            {"Condition": "OpsEnabled"},
            {"Fn::Not": [{"Fn::Equals": [{"Ref": "MonthlyBudgetUsd"}, "0"]}]},
        ]
    }


# ---------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------


def test_ops_topic_arn_output_present_and_conditioned_on_ops_enabled() -> None:
    """I5 fix (final-fix-report.md): the OUTPUT itself now carries
    `Condition: OpsEnabled` (CloudFormation's own documented mechanism for
    a conditionally-present output -- the whole entry is omitted, not
    printed blank, when the condition is false), and the VALUE is the
    real, unwrapped topic ARN -- no more Fn::If-wrapped-to-"" value on an
    unconditioned output (the shape that used to trip cfn-lint/CDK's own
    synth validation W1001)."""
    t = synth_template()
    outs = t.to_json()["Outputs"]
    assert "OpsTopicArn" in outs
    assert outs["OpsTopicArn"]["Condition"] == "OpsEnabled"
    assert "Fn::If" not in json.dumps(outs["OpsTopicArn"]["Value"])


def test_ops_dashboard_url_output_present_and_conditioned_on_ops_enabled() -> None:
    t = synth_template()
    outs = t.to_json()["Outputs"]
    assert "OpsDashboardUrl" in outs
    assert outs["OpsDashboardUrl"]["Condition"] == "OpsEnabled"
    value = outs["OpsDashboardUrl"]["Value"]
    assert "Fn::If" not in json.dumps(value)
    joined = value["Fn::Join"][1]
    assert "cloudwatch/home" in "".join(str(part) for part in joined)


# ---------------------------------------------------------------------
# Part A hygiene, re-verified from the ops-layer angle: log groups, DLQ,
# Data API. (Construct-specific hygiene assertions also live in
# test_network_db.py / test_services.py / test_migration_runner.py.)
# ---------------------------------------------------------------------


def test_four_log_groups_all_thirty_day_retention_and_destroy() -> None:
    """Gate, migration runner, MCP container, embedder container -- one
    explicit `logs.LogGroup` each, never `log_retention=`."""
    t = synth_template()
    log_groups = t.find_resources("AWS::Logs::LogGroup")
    assert len(log_groups) == 4
    for lg in log_groups.values():
        assert lg["Properties"]["RetentionInDays"] == 30
        assert lg["DeletionPolicy"] == "Delete"


def test_gate_function_log_group_uses_the_derived_name() -> None:
    t = synth_template()
    log_groups = t.find_resources("AWS::Logs::LogGroup")
    named = [lg for lg in log_groups.values() if lg["Properties"].get("LogGroupName")]
    assert len(named) == 1
    assert named[0]["Properties"]["LogGroupName"] == "/aws/lambda/fde-gate-service"


def test_gate_function_uses_explicit_log_group_not_log_retention() -> None:
    t = synth_template()
    template = t.to_json()
    fn = next(
        v
        for v in template["Resources"].values()
        if v["Type"] == "AWS::Lambda::Function"
        and v["Properties"].get("Handler") == "fde_gate.handler.lambda_handler"
    )
    assert "LoggingConfig" in fn["Properties"]
    # No LogRetention custom-resource framework Lambda anywhere (that would
    # be a bootstrap-violating asset Lambda -- see gate.py's own comment).
    assert not any("LogRetention" in k for k in template["Resources"])


def test_both_gate_schedules_carry_the_shared_dlq() -> None:
    t = synth_template()
    template = t.to_json()
    queues = {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::SQS::Queue"}
    dlq_id = next(k for k, v in queues.items() if v["Properties"].get("QueueName") == "fde-ops-dlq")
    rules = t.find_resources("AWS::Events::Rule")
    gate_rules = [
        r
        for r in rules.values()
        if r["Properties"]["ScheduleExpression"] in ("rate(1 minute)", "rate(1 hour)")
    ]
    assert len(gate_rules) == 2
    for rule in gate_rules:
        target = rule["Properties"]["Targets"][0]
        assert target["DeadLetterConfig"] == {"Arn": {"Fn::GetAtt": [dlq_id, "Arn"]}}
        assert target["RetryPolicy"] == {"MaximumRetryAttempts": 2}


def test_database_backup_retention_and_data_api_enabled() -> None:
    t = synth_template()
    cluster = next(iter(t.find_resources("AWS::RDS::DBCluster").values()))
    assert cluster["Properties"]["BackupRetentionPeriod"] == 7
    assert cluster["Properties"]["EnableHttpEndpoint"] is True


def test_migration_role_has_namespace_conditioned_put_metric_data() -> None:
    t = synth_template()
    template = t.to_json()
    roles = {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::IAM::Role"}
    migration_role_id = next(
        k
        for k, v in roles.items()
        for policy in v["Properties"].get("Policies", [])
        if policy["PolicyName"] == "migration-permissions"
    )
    statements = [
        stmt
        for policy in roles[migration_role_id]["Properties"]["Policies"]
        for stmt in policy["PolicyDocument"]["Statement"]
    ]
    put_metrics = next(s for s in statements if s.get("Sid") == "PutOpsMetrics")
    assert put_metrics["Action"] == "cloudwatch:PutMetricData"
    assert put_metrics["Condition"] == {"StringEquals": {"cloudwatch:namespace": "FDE/Platform"}}
