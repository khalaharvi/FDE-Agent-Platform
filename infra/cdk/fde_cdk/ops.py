"""`OpsLayer`: the pluggable, user-replaceable ops/alarm package.

Design stance (binding, set by a prod-ops design review -- see the task
brief and `docs/superpowers/plans/2026-08-02-one-click-aws-deploy.md`'s
Task 7.5 amendment): this package must be drop-in replaceable by the
adopter's own ops/health stack. The SNS topic (`fde-ops`) is the
integration seam -- "subscribe Datadog/PagerDuty/your SIEM here; set
OpsMode=topic-only to skip email" (docs/13). Everything else in this
construct (the 8 alarms, the dashboard, the budget, the default email
subscription) is AWS-specific convenience wired to that same topic, never
the other way around -- nothing downstream of the topic is load-bearing for
anything upstream of it.

Condition-gating mechanism
---------------------------
Every resource this construct creates carries `Condition: OpsEnabled`
(`OpsMode != "off"`, `params.py`). `CfnCondition`s cannot be branched on in
Python (a Python `if params.ops_enabled: ...` would bake in whichever
branch happened to be live at `cdk synth` time, not what a launcher later
picks in the console -- same reasoning `database.py`'s own module docstring
gives for its tier-switching `Fn::If`s). So every resource here is built
"for real" through its L2 (or, for `AWS::Budgets::Budget`, which has no
L2, directly as an L1), and the condition is applied afterward via the
established escape hatch: `<l2>.node.default_child.cfn_options.condition =
params.ops_enabled` for L2s, `<l1>.cfn_options.condition = params.
ops_enabled` directly for the one L1 (`CfnBudget`).

The one non-obvious part of "every resource" (see `_condition_rule_target`
below): `events.Rule.add_target(events_targets.LambdaFunction(...))` makes
CDK synthesize an `AWS::Lambda::Permission` granting `events.amazonaws.com`
`lambda:InvokeFunction` on the invoked function -- and that `CfnPermission`
is added as a CHILD OF THE RULE's own construct node (verified via a
standalone synth), not of the Lambda function. Since the rule itself
carries `Condition: OpsEnabled`, this generated permission has to be found
and conditioned too, or CloudFormation would try to create a resource
referencing (via `SourceArn: Fn::GetAtt`) a conditionally-absent rule
outside the condition that actually gates the rule's existence -- a
template CloudFormation itself rejects at deploy time, not just a
cfn-lint nitpick.

Alarm timing choices (not fixed by the brief, so documented here)
-------------------------------------------------------------------
The brief pins each alarm's metric/threshold/comparison exactly, and gives
an explicit period/evaluation-period multiplier for three of the eight
("3x1min", "3x1min", "15min" on the ACU/backlog pair). For the alarms
where the brief states a threshold with no explicit time-window multiplier
("X ~5/5min" for target 5xx is explicit; the rest -- migration errors,
DB connections, DLQ messages -- are not), this module applies one
consistent, simple rule: a period equal to any duration the brief DOES
state (5min, 15min), evaluated over a single period (`evaluation_periods=
1`); and, absent any stated duration at all, a 5-minute period with
`evaluation_periods=1` (CloudWatch's own default granularity, and
appropriate for alarms on rare, individually-significant events -- a
Lambda invoked only at deploy time (migration errors) or only when
something is already stuck (DLQ backlog) should not need repeated breaches
to confirm).

Aurora DB-connections threshold derivation
--------------------------------------------
"80% of the 2-ACU cap" is computed, not hand-typed, from Aurora
PostgreSQL's own default `max_connections` formula (visible in the
`default.aurora-postgresql16` parameter group): `LEAST({DBInstanceClass
Memory/9531392}, 5000)`. Aurora Serverless v2 provisions ~2 GiB of memory
per ACU (AWS's documented ACU-to-memory ratio); `database.py`'s
`serverless_v2_min_capacity=2` is the worst-case FLOOR this cluster can
scale down to under low load, so it is also the connection-count floor
this alarm defends -- using the max capacity (8 ACU) instead would let
real usage silently blow past the floor's actual ceiling without ever
tripping this alarm. `2 ACU * 2 GiB = 4 GiB = 4294967296 bytes;
4294967296 // 9531392 = 450` max connections at the floor; `450 * 0.8 =
360`.
"""

from __future__ import annotations

import aws_cdk as cdk
import aws_cdk.aws_budgets as budgets
import aws_cdk.aws_cloudwatch as cloudwatch
import aws_cdk.aws_cloudwatch_actions as cw_actions
import aws_cdk.aws_ecs as ecs
import aws_cdk.aws_elasticloadbalancingv2 as elbv2
import aws_cdk.aws_events as events
import aws_cdk.aws_events_targets as events_targets
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_rds as rds
import aws_cdk.aws_sns as sns
import aws_cdk.aws_sns_subscriptions as sns_subscriptions
import aws_cdk.aws_sqs as sqs
from constructs import Construct

from fde_cdk.params import LaunchParams, ops_alert_email_value

# Task 7.5's own brief: the EventBridge rule that keeps
# `FdeEmbedQueueBacklog` fed points at the SAME migration-runner Lambda,
# with this exact payload -- `lambdas/migration_runner/handler.py`'s
# `_OPS_METRICS_SOURCE`/`is_ops_metrics_event` is the other, hand-copied
# half of this contract (see that module's own comment for why it cannot
# be a shared import: the handler ships standalone in a Lambda zip).
_OPS_METRICS_EVENT = {"source": "fde.ops.metrics"}
_OPS_METRICS_NAMESPACE = "FDE/Platform"
_OPS_METRICS_METRIC_NAME = "EmbedQueueDepth"

# See module docstring's "Aurora DB-connections threshold derivation".
_SERVERLESS_MIN_ACU = 2
_BYTES_PER_ACU = 2 * 1024 * 1024 * 1024  # ~2 GiB/ACU, AWS's documented ratio
_MAX_CONNECTIONS_AT_FLOOR = (_SERVERLESS_MIN_ACU * _BYTES_PER_ACU) // 9_531_392  # 450
_DB_CONNECTIONS_THRESHOLD = _MAX_CONNECTIONS_AT_FLOOR * 0.8  # 360.0


class OpsLayer(Construct):
    """`topic`: the `fde-ops` SNS topic (THE integration seam -- see module
    docstring). `dashboard`: the `FdeOps` CloudWatch dashboard. Eight
    alarms (see module docstring for the full inventory), one email
    subscription, one EventBridge rule feeding the embed-queue-depth
    custom metric, and one AWS Budget -- every resource here carries
    `Condition: OpsEnabled`.

    Takes handles to resources built by earlier constructs (`gate_function`,
    `migration_function`, `mcp_target_group`, `mcp_service`,
    `embedder_service`, `db_cluster`, `dlq`) rather than reaching into
    sibling constructs itself, matching how every other construct in this
    stack takes its cross-construct dependencies as keyword arguments (see
    `stack.py`).
    """

    topic: sns.Topic
    dashboard: cloudwatch.Dashboard
    topic_arn: str
    dashboard_url: str

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        params: LaunchParams,
        gate_function: lambda_.Function,
        migration_function: lambda_.Function,
        mcp_target_group: elbv2.ApplicationTargetGroup,
        mcp_service: ecs.FargateService,
        embedder_service: ecs.FargateService,
        db_cluster: rds.DatabaseCluster,
        dlq: sqs.Queue,
    ) -> None:
        super().__init__(scope, construct_id)

        ops_enabled = params.ops_enabled

        # --- The seam: SNS topic ---
        self.topic = sns.Topic(
            self,
            "Topic",
            topic_name="fde-ops",
            display_name="FDE Agent Platform ops alerts",
        )
        self._condition(self.topic, ops_enabled)

        # --- Email subscription: OpsMode=email only ---
        subscription = self.topic.add_subscription(
            sns_subscriptions.EmailSubscription(ops_alert_email_value(params))
        )
        self._condition(subscription, params.ops_email_enabled)

        # --- Eight alarms ---
        self._alarm(
            "FdeGateErrors",
            metric=gate_function.metric_errors(period=cdk.Duration.minutes(1)),
            threshold=3,
            evaluation_periods=3,
            description=(
                "fde-gate-service Lambda Errors >= 3 in any of 3 consecutive 1-minute periods."
            ),
            ops_enabled=ops_enabled,
        )
        self._alarm(
            "FdeMigrationRunnerErrors",
            metric=migration_function.metric_errors(period=cdk.Duration.minutes(5)),
            threshold=1,
            evaluation_periods=1,
            description="migration-runner Lambda Errors >= 1 -- any failure here is significant.",
            ops_enabled=ops_enabled,
        )
        self._alarm(
            "FdeMcpUnhealthyHosts",
            metric=mcp_target_group.metrics.unhealthy_host_count(period=cdk.Duration.minutes(1)),
            threshold=1,
            evaluation_periods=3,
            description=(
                "fde-mcp ALB target group UnHealthyHostCount >= 1 in any of 3 "
                "consecutive 1-minute periods."
            ),
            ops_enabled=ops_enabled,
        )
        self._alarm(
            "FdeMcpTarget5xx",
            metric=mcp_target_group.metrics.http_code_target(
                elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
                period=cdk.Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=5,
            evaluation_periods=1,
            description="fde-mcp ALB target group HTTPCode_Target_5XX_Count >= 5 in 5 minutes.",
            ops_enabled=ops_enabled,
        )
        self._alarm(
            "FdeDbAcuCeiling",
            metric=db_cluster.metric_serverless_database_capacity(
                period=cdk.Duration.minutes(15), statistic="Average"
            ),
            threshold=7.5,
            evaluation_periods=1,
            description="Aurora ServerlessDatabaseCapacity average >= 7.5 ACU over 15 minutes.",
            ops_enabled=ops_enabled,
        )
        self._alarm(
            "FdeDbConnections",
            metric=db_cluster.metric_database_connections(
                period=cdk.Duration.minutes(5), statistic="Average"
            ),
            threshold=_DB_CONNECTIONS_THRESHOLD,
            evaluation_periods=1,
            description=(
                "Aurora DatabaseConnections average >= 80% of the 2-ACU-floor "
                "max_connections estimate (360) -- see module docstring for the derivation."
            ),
            ops_enabled=ops_enabled,
        )
        self._alarm(
            "FdeEmbedQueueBacklog",
            metric=cloudwatch.Metric(
                namespace=_OPS_METRICS_NAMESPACE,
                metric_name=_OPS_METRICS_METRIC_NAME,
                period=cdk.Duration.minutes(15),
                statistic="Average",
            ),
            threshold=500,
            evaluation_periods=1,
            description="FDE/Platform:EmbedQueueDepth average >= 500 over 15 minutes.",
            ops_enabled=ops_enabled,
        )
        self._alarm(
            "FdeOpsDlqMessages",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(5), statistic="Maximum"
            ),
            threshold=1,
            evaluation_periods=1,
            description=(
                "fde-ops-dlq ApproximateNumberOfMessagesVisible >= 1 -- something is stuck."
            ),
            ops_enabled=ops_enabled,
        )

        # --- Embed-queue-depth metrics feed: EventBridge -> migration Lambda ---
        metrics_rule = events.Rule(
            self,
            "OpsMetricsRule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            description=(
                "Probe kg.embed_queue's pending-row count and publish it as "
                f"{_OPS_METRICS_NAMESPACE}:{_OPS_METRICS_METRIC_NAME} for FdeEmbedQueueBacklog."
            ),
        )
        metrics_rule.add_target(
            events_targets.LambdaFunction(
                migration_function, event=events.RuleTargetInput.from_object(_OPS_METRICS_EVENT)
            )
        )
        self._condition(metrics_rule, ops_enabled)
        # See module docstring: the AWS::Lambda::Permission `add_target`
        # synthesizes lands as a child of the RULE, not the function --
        # find and condition it explicitly, or it would reference a
        # conditionally-absent rule from an unconditioned resource.
        for child in metrics_rule.node.children:
            if isinstance(child, lambda_.CfnPermission):
                child.cfn_options.condition = ops_enabled

        # --- Dashboard ---
        self.dashboard = cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name="FdeOps",
            widgets=[
                [
                    cloudwatch.GraphWidget(
                        title="fde-gate-service",
                        left=[gate_function.metric_invocations(), gate_function.metric_errors()],
                    )
                ],
                [
                    cloudwatch.GraphWidget(
                        title="fde-mcp (ALB + service)",
                        left=[
                            mcp_target_group.metrics.unhealthy_host_count(),
                            mcp_target_group.metrics.http_code_target(
                                elbv2.HttpCodeTarget.TARGET_5XX_COUNT
                            ),
                        ],
                        right=[mcp_service.metric_cpu_utilization()],
                    )
                ],
                [
                    cloudwatch.GraphWidget(
                        title="fde-embedder + embed-queue depth",
                        left=[embedder_service.metric_cpu_utilization()],
                        right=[
                            cloudwatch.Metric(
                                namespace=_OPS_METRICS_NAMESPACE,
                                metric_name=_OPS_METRICS_METRIC_NAME,
                            )
                        ],
                    )
                ],
                [
                    cloudwatch.GraphWidget(
                        title="Aurora (fde)",
                        left=[db_cluster.metric_serverless_database_capacity()],
                        right=[db_cluster.metric_database_connections()],
                    )
                ],
                [
                    cloudwatch.GraphWidget(
                        title="migration-runner",
                        left=[
                            migration_function.metric_invocations(),
                            migration_function.metric_errors(),
                        ],
                    )
                ],
            ],
        )
        self._condition(self.dashboard, ops_enabled)

        # --- Budget: OpsMode != off AND MonthlyBudgetUsd != 0 ---
        budget = budgets.CfnBudget(
            self,
            "Budget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_name="fde-platform-monthly-budget",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=params.monthly_budget_usd.value_as_number, unit="USD"
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=ops_alert_email_value(params), subscription_type="EMAIL"
                        )
                    ],
                )
            ],
        )
        budget.cfn_options.condition = params.ops_budget_enabled

        # --- Outputs the stack prints (values, not the CfnOutputs
        # themselves -- outputs.py builds those, same separation-of-
        # concerns as every other construct's public attributes) ---
        # `OpsTopicArn`: present in the template unconditionally per the
        # brief ("Output OpsTopicArn always"), but its VALUE resolves to ""
        # when OpsMode=off (the topic resource itself does not exist then)
        # -- same Fn::If-wrapped-value-over-conditionally-absent-resource
        # pattern `params.dynamic_secret_env_value` already documents and
        # relies on (CloudFormation never evaluates the untaken Fn::If
        # branch).
        self.topic_arn = cdk.Token.as_string(
            cdk.Fn.condition_if(ops_enabled.logical_id, self.topic.topic_arn, "")
        )
        dashboard_url = cdk.Fn.join(
            "",
            [
                "https://",
                cdk.Aws.REGION,
                ".console.aws.amazon.com/cloudwatch/home?region=",
                cdk.Aws.REGION,
                "#dashboards:name=",
                self.dashboard.dashboard_name,
            ],
        )
        self.dashboard_url = cdk.Token.as_string(
            cdk.Fn.condition_if(ops_enabled.logical_id, dashboard_url, "")
        )

    @staticmethod
    def _condition(construct: Construct, condition: cdk.CfnCondition) -> None:
        """`<l2>.node.default_child.cfn_options.condition = condition` --
        the module docstring's escape hatch, factored out since every L2
        resource this construct builds needs exactly this one line."""
        cfn_resource = construct.node.default_child
        assert cfn_resource is not None
        cfn_resource.cfn_options.condition = condition  # type: ignore[attr-defined]

    def _alarm(
        self,
        alarm_name: str,
        *,
        metric: cloudwatch.IMetric,
        threshold: float,
        evaluation_periods: int,
        description: str,
        ops_enabled: cdk.CfnCondition,
    ) -> cloudwatch.Alarm:
        """One alarm, wired to `self.topic`, conditioned on `OpsEnabled`.
        `alarm_name` is used as BOTH the construct id and the CFN
        `AlarmName` property -- the brief's eight names are meant to be the
        literal, human-visible CloudWatch alarm names, not just internal
        CDK logical ids."""
        alarm = cloudwatch.Alarm(
            self,
            alarm_name,
            alarm_name=alarm_name,
            alarm_description=description,
            metric=metric,
            threshold=threshold,
            evaluation_periods=evaluation_periods,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(self.topic))
        self._condition(alarm, ops_enabled)
        return alarm
