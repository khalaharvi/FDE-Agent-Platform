"""Aurora PostgreSQL (pgvector) database construct.

Engine floor: docs/09-deployment.md names "Aurora PostgreSQL 16.8+ ...
pgvector >= 0.8.0 is a hard floor", and `db/001`'s own extension guard
(`SELECT extversion FROM pg_extension WHERE extname = 'vector'`) refuses to
apply any migration below pgvector 0.8.0 -- that version is where
`hnsw.iterative_scan` ships, which the retrieval SQL in `db/008` depends on.
`AuroraPostgresEngineVersion.VER_16_8` exists on the pinned aws-cdk-lib
(2.263.0), so no `.of("16.8", "16")` string-literal fallback is needed here;
if a future lib bump ever drops the enum member, restore that fallback with
this same citation.

Tier switching (`params.deploy_tier` / `params.is_production`): a
`CfnCondition` is a CloudFormation template-time value -- it is resolved when
someone launches the stack, not when this Python module runs at `cdk synth`.
A Python-level `if params.is_production: ...` here would silently pick
whichever branch happened to be true during THIS synth and bake only that
one tier into the template, no matter what a user later selects for
`DeployTier` in the console. So every tier difference below is emitted as a
CloudFormation `Fn::If` in the synthesized template, via the L1 escape hatch
(`<l2>.node.default_child.add_property_override(...)`) -- never a Python
conditional on `params.is_production` itself. One `rds.DatabaseCluster` is
built with a Serverless v2 writer (the demo-tier shape); its L1
`CfnDBInstance`'s `DBInstanceClass` and the cluster's L1 `DeletionProtection`
are then overridden with `Fn::If(IsProduction, ...)` so CloudFormation picks
the live branch at launch time. This reads more cleanly than a parallel
L1-only `CfnDBCluster` + `CfnDBInstance` pair (subnet group, parameter
group, and secret wiring all come free from the L2) at the cost of building
one branch "for real" through the L2 first -- acceptable since only
literal, inert properties (an instance class string, a boolean) are
overridden, not anything that changes which child resources exist.
"""

from __future__ import annotations

import aws_cdk as cdk
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_rds as rds
import aws_cdk.aws_secretsmanager as secretsmanager
from constructs import Construct

from fde_cdk.params import LaunchParams


class Database(Construct):
    """Aurora pgvector cluster living in `vpc`'s private-with-egress
    subnets, with its own security group (ingress opened by later tasks'
    constructs, e.g. the gate Lambda and AgentCore runtimes -- not here)."""

    cluster: rds.DatabaseCluster
    secret: secretsmanager.ISecret
    security_group: ec2.SecurityGroup

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        params: LaunchParams,
        vpc: ec2.IVpc,
    ) -> None:
        super().__init__(scope, construct_id)

        self.security_group = ec2.SecurityGroup(
            self,
            "SecurityGroup",
            vpc=vpc,
            description=(
                "Aurora pgvector cluster -- no ingress rules added here; "
                "later tasks (gate service, agent runtimes) open 5432 from "
                "their own security groups via add_ingress_rule."
            ),
            allow_all_outbound=False,
        )

        # Demo-tier shape: Serverless v2. Production overrides the instance
        # class below via Fn::If; the writer's *logical* CFN resource is the
        # same CfnDBInstance either way; only its DBInstanceClass differs.
        writer = rds.ClusterInstance.serverless_v2("Writer")

        self.cluster = rds.DatabaseCluster(
            self,
            "Cluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_8
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[self.security_group],
            default_database_name="fde",
            writer=writer,
            # Demo-tier scaling floor. docs/09: Aurora Serverless v2 can
            # scale ACUs (and the share of memory available to
            # shared_buffers) down far enough to evict an in-progress HNSW
            # index build; min=2 ACU is the floor that keeps shared_buffers
            # big enough to hold it, not an arbitrary cheap default.
            # max=8 is this stack's cost ceiling for the demo tier.
            # This property is emitted unconditionally (not Fn::If'd):
            # RDS only consults it when a serverless instance exists in the
            # cluster, so it is inert -- not wrong -- whenever the writer's
            # instance class below resolves to the production branch
            # ("db.r6g.xlarge") instead of "db.serverless".
            serverless_v2_min_capacity=2,
            serverless_v2_max_capacity=8,
            # Snapshot-on-delete ALWAYS, in both tiers. This is a
            # `RemovalPolicy` -- a CDK/synth-time concept, not a
            # CloudFormation template-time value -- so, unlike
            # DeletionProtection below, it is fine to set directly from
            # Python rather than through the Fn::If pattern.
            removal_policy=cdk.RemovalPolicy.SNAPSHOT,
        )

        # --- Tier switching: L1 escape hatch, Fn::If on both branches ---
        # `params.is_production` is a CfnCondition; `Fn.condition_if` is
        # CloudFormation's own ternary (`Fn::If`), keyed by the condition's
        # logical ID. Both branches land in the ONE emitted template and
        # CloudFormation itself picks the live one when the stack launches
        # -- a Python `if` on `params.is_production` cannot do this, see
        # module docstring.
        writer_child = self.cluster.node.find_child("Writer")
        cfn_writer = writer_child.node.default_child
        assert cfn_writer is not None
        cfn_writer.add_property_override(
            "DBInstanceClass",
            cdk.Fn.condition_if(
                params.is_production.logical_id,
                "db.r6g.xlarge",  # production: provisioned
                "db.serverless",  # demo: Aurora Serverless v2 (see writer= above)
            ),
        )

        cfn_cluster = self.cluster.node.default_child
        assert cfn_cluster is not None
        cfn_cluster.add_property_override(
            "DeletionProtection",
            cdk.Fn.condition_if(params.is_production.logical_id, True, False),
        )

        # `cluster.secret`: since no `credentials=` override was passed
        # above, the L2 generates one Secrets Manager secret for the
        # cluster's master credentials (`Credentials.from_generated_secret`
        # is CDK's default) and attaches it via
        # `AWS::SecretsManager::SecretTargetAttachment` -- exactly one
        # `AWS::SecretsManager::Secret` resource
        # (`test_db_secret_is_rds_managed_shape` pins that count), in the
        # same `{"host","port","username","password","dbname"}` rotation
        # shape `fde_mcp.db._dsn_from_secrets_manager` parses. At synth
        # time the secret's JSON only carries `username`/`password` --
        # `host`/`port`/`dbname` are populated once standard RDS rotation
        # runs (`cluster.add_rotation_single_user()`, wired in a later
        # task). Until then, `_dsn_from_secrets_manager`'s own tolerant
        # fallbacks (`secret.get("host") or settings.host`, same pattern
        # for `dbname`) are satisfied by the `FDE_DB_HOST` / `FDE_DB_NAME`
        # env vars a later task sets on the consuming Lambda/runtime
        # alongside this secret's ARN (`FDE_DB_SECRET_ARN`).
        assert self.cluster.secret is not None
        self.secret = self.cluster.secret
