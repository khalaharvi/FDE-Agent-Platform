"""The forward-only migration-runner Lambda + its CloudFormation custom
resource.

Two things this construct has to get right that no earlier task's
construct needed to:

1. **Code from S3, never a CDK asset.** `lambda_.Code.from_asset(...)`
   (the normal way to hand CDK a local directory of Lambda code) publishes
   through the CDK bootstrap assets bucket -- exactly the thing
   `BootstraplessSynthesizer` (`fde_cdk/stack.py`) and
   `tests/test_synth.py::test_no_cdk_assets_anywhere` exist to forbid. The
   Lambda's code has to already be sitting in S3 (`build.py` produces the
   zip; `release.yml`, not this repo, uploads it) at
   `releases/{ReleaseTag}/migration-runner.zip` in the assets bucket
   `params` resolves -- see the `Fn::If` resolution below, which is the
   piece Task 2 deferred here (`AssetsBucket`/`AssetsRegionMap` existed
   from Task 2 onward but had no consumer until now).

2. **A Provider-free custom resource.** The standard CDK way to back a
   custom resource (`aws_cdk.custom_resources.Provider`) builds ITS OWN
   framework Lambdas (onEvent/isComplete/waiter) from a CDK asset -- the
   same bootstrap-free violation as (1), just one level removed. Instead,
   `cdk.CustomResource(service_token=fn.function_arn)` is used directly:
   this synthesizes a plain `AWS::CloudFormation::CustomResource` whose
   `ServiceToken` points straight at `Migrations.function`, with no
   framework Lambda in between. The cost of skipping the Provider
   framework is that `lambdas/migration_runner/handler.py` has to
   implement the cfn-response protocol itself (`_send_cfn_response`,
   plain `urllib.request`) rather than getting it for free.
"""

from __future__ import annotations

import aws_cdk as cdk
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_iam as iam
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_rds as rds
import aws_cdk.aws_s3 as s3
import aws_cdk.aws_secretsmanager as secretsmanager
from constructs import Construct

from fde_cdk.params import LaunchParams

_HANDLER_ENTRYPOINT = "handler.handler"
# Lambda's own ceiling (15 minutes); 15 migrations + role/secret work fits easily inside it.
_TIMEOUT = cdk.Duration.seconds(900)


class Migrations(Construct):
    """`function`: the VPC-attached migration-runner Lambda (python3.12,
    arm64, code from S3). `resource`: the `AWS::CloudFormation::
    CustomResource` that invokes it once per `Create`/`Update`, with
    `ReleaseTag` as one of its properties so a new release tag (a new zip
    key) reliably triggers a re-run even if nothing else about the stack
    changed.
    """

    function: lambda_.Function
    resource: cdk.CustomResource

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        params: LaunchParams,
        vpc: ec2.IVpc,
        db_secret: secretsmanager.ISecret,
        db_cluster: rds.DatabaseCluster,
        migration_role: iam.IRole,
    ) -> None:
        super().__init__(scope, construct_id)

        # --- Fn::If bucket resolution (deferred from Task 2 to here, the
        # first and only consumer of AssetsBucket/AssetsRegionMap) ---
        # `Fn.condition_if` returns a jsii IResolvable, not a plain Python
        # str -- `Bucket.from_bucket_name`'s generated type-check rejects
        # that outright (verified). `cdk.Token.as_string(...)` wraps it
        # into the same kind of token-embedded str `cdk.Aws.REGION` already
        # is elsewhere in this codebase (e.g. `identity.py`'s
        # `discovery_url`), which the L2 accepts and resolves at synth time
        # into the exact `Fn::If`/`Fn::FindInMap` structure (verified via a
        # standalone synth: `S3Bucket` ends up as `{"Fn::If": [
        # "HasAssetsBucket", {"Ref": "AssetsBucket"}, {"Fn::FindInMap": [
        # "AssetsRegionMap", {"Ref": "AWS::Region"}, "bucket"]}]}`) -- no L1
        # `add_property_override` escape hatch needed.
        code_bucket_name = cdk.Token.as_string(
            cdk.Fn.condition_if(
                params.has_assets_bucket.logical_id,
                params.assets_bucket.value_as_string,
                cdk.Fn.find_in_map(params.assets_region_map.logical_id, cdk.Aws.REGION, "bucket"),
            )
        )
        code_bucket = s3.Bucket.from_bucket_name(self, "AssetsBucketRef", code_bucket_name)
        code_key = f"releases/{params.release_tag.value_as_string}/migration-runner.zip"

        self.function = lambda_.Function(
            self,
            "Function",
            description=(
                "Forward-only migration runner: applies db/0*.sql, mints the "
                "three login-user secrets, seeds the admin reviewer."
            ),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler=_HANDLER_ENTRYPOINT,
            code=lambda_.Code.from_bucket(code_bucket, code_key),
            timeout=_TIMEOUT,
            memory_size=512,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            role=migration_role,
            environment={"DB_SECRET_ARN": db_secret.secret_arn},
        )

        # `Database.security_group`'s own docstring names this construct
        # exactly: "ingress opened by later tasks' constructs ... via
        # add_ingress_rule" -- `allow_default_port_from` is that call,
        # using the cluster's own default port (5432) rather than a
        # hand-typed `ec2.Port.tcp(5432)` so this can never drift from
        # whatever port the cluster actually listens on.
        db_cluster.connections.allow_default_port_from(
            self.function, "migration-runner Lambda applies db/0*.sql at deploy time"
        )

        # --- Provider-free custom resource (see module docstring) ---
        self.resource = cdk.CustomResource(
            self,
            "Resource",
            service_token=self.function.function_arn,
            properties={
                # `ReleaseTag` changing is what makes CloudFormation
                # re-invoke this custom resource as an Update on a new
                # release: custom-resource re-invocation is driven by ITS
                # OWN properties diffing, not by whatever the backing
                # function's code happens to be -- pinning only the
                # function's `S3Key` (which does vary by tag already)
                # would NOT by itself cause a re-run.
                "ReleaseTag": params.release_tag.value_as_string,
                "AdminEmail": params.admin_email.value_as_string,
            },
        )
