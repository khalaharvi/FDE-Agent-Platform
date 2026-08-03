"""`GateService`: the `fde-gate-service` Lambda, its `apigatewayv2.HttpApi`
front door, and the two EventBridge schedules that keep the workflow runner
correct.

The CDK shapes here mirror `packages/fde-gate/src/fde_gate/deploy/{lambda_fn,
api,schedule}.py` exactly -- those three scripts are what a human operator
would run by hand against a non-CDK account; this construct is the same
configuration expressed as CloudFormation instead of boto3 calls. Read all
three before changing anything here.

Cross-construct ordering (see the plan's own note, and `docs/superpowers/
plans/2026-08-02-one-click-aws-deploy.md`'s Task 6 section): the *eventual*
construct order is Migrations -> Services -> Agents (Task 7) -> GateService,
because the Lambda's `FDE_RUNTIME_ARN_*` env vars need the three AgentCore
runtime ARNs Task 7's `Agents` construct creates. Task 7 does not exist yet.
`GateService` accepts `runtime_arns: dict[str, str] | None = None`; when it
is `None` (this task's stack wiring), every `FDE_RUNTIME_ARN_{AGENT}` env
var is set to `""` rather than omitted, so the Lambda's environment shape
(the set of keys) does not change once Task 7 supplies real values --
only the values do. Task 7 re-wires `stack.py` to construct `Agents` before
`GateService` and pass `agents.runtime_arns` through.

The `FDE_DB_SECRET_ARN` / `gate_role` mismatch this construct works around
------------------------------------------------------------------------
`IamRoles.gate_role` (`iam_roles.py`, Task 4) already carries a
`ReadTheDbSecret` statement -- but its `Resource` was substituted with
`db_secret.secret_arn`, i.e. the Aurora cluster's own *master* credentials
secret (`Database.secret`), not `fde/db/gate` (the narrowly-scoped login
secret the migration-runner custom resource mints for this service, per
Task 5's `lambdas/migration_runner/handler.py`). That substitution was the
only thing `iam_roles.py` *could* reference at the time it was written:
`fde/db/gate` does not exist as a CDK object anywhere in this tree -- it is
created by a Lambda-backed custom resource at deploy time, not synthesized
as a `secretsmanager.Secret` construct Task 4 could have taken a token from.
Left alone, the deployed gate Lambda would set `FDE_DB_SECRET_ARN` to
`fde/db/gate` (per this task's own brief) while its execution role could
only read the cluster master secret -- a `GetSecretValue` `AccessDenied` on
every request, discovered here by reading `gate-lambda-permissions-policy.
json`'s substitution against `iam_roles.py`, not by any earlier task's own
tests (none of them pin `ReadTheDbSecret`'s `Resource` value). `GateService`
adds one supplemental inline-policy statement to the *existing* `gate_role`
object (not a new role -- the role's log-group/self-invoke statements are
already scoped to `GATE_FUNCTION_NAME`, which this construct's Lambda must
still use verbatim) granting `secretsmanager:GetSecretValue` on
`fde_db_secrets_wildcard_arn()` (the same `fde/db/*` pattern
`migration_role` already uses to *mint* the three secrets). This is
additive only -- nothing `iam_roles.py` already granted is narrowed or
removed.
"""

from __future__ import annotations

import aws_cdk as cdk
import aws_cdk.aws_apigatewayv2 as apigwv2
import aws_cdk.aws_apigatewayv2_authorizers as apigwv2_authorizers
import aws_cdk.aws_apigatewayv2_integrations as apigwv2_integrations
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_events as events
import aws_cdk.aws_events_targets as events_targets
import aws_cdk.aws_iam as iam
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_rds as rds
import aws_cdk.aws_s3 as s3
import aws_cdk.aws_secretsmanager as secretsmanager
from constructs import Construct

from fde_cdk.iam_roles import GATE_FUNCTION_NAME
from fde_cdk.identity import Identity
from fde_cdk.params import (
    LaunchParams,
    dynamic_secret_env_value,
    fde_db_secrets_wildcard_arn,
    resolve_assets_bucket_name,
)

# Matches lambda_fn.py's DEFAULT_HANDLER/DEFAULT_TIMEOUT_S/DEFAULT_MEMORY_MB
# exactly -- see that file's own module docstring for why each value is
# what it is (900s: an async self-invoke executes a whole agent step;
# arm64: must match what package.py built for).
_HANDLER_ENTRYPOINT = "fde_gate.handler.lambda_handler"
_TIMEOUT = cdk.Duration.seconds(900)
_MEMORY_MB = 512

# The login secret infra/cdk/lambdas/migration_runner/handler.py's
# LOGIN_SECRETS mints for this service. A fixed literal, not a CDK token:
# it is created by a custom resource this construct has no Python object
# for (see the module docstring above).
_GATE_DB_SECRET_NAME = "fde/db/gate"

# The three agents `fde_gate.config.GateSettings.runtime_arn_for` looks up
# by name (packages/fde-gate/src/fde_gate/config.py) -- fixed here so the
# placeholder-env-var shape (see module docstring) matches exactly what
# Task 7's `Agents.runtime_arns` will eventually supply keys for.
_AGENT_NAMES = ("ENGAGEMENT", "WORKFLOW", "DEVELOPMENT")

# (logical id suffix, schedule expression, event payload, description) --
# mirrors packages/fde-gate/src/fde_gate/deploy/schedule.py's RULES tuple
# exactly (rule name there becomes this construct's id; EventBridge rule
# *names* are left to CloudFormation's auto-generated ones since nothing
# downstream depends on the CLI tool's literal "fde-gate-tick" name the way
# gate_role's statements depend on GATE_FUNCTION_NAME).
_SCHEDULES: tuple[tuple[str, str, dict[str, str], str], ...] = (
    (
        "GateTickRule",
        "rate(1 minute)",
        {"source": "fde.gate.tick"},
        "Time out overdue workflow steps and advance idle runs",
    ),
    (
        "GateExpiryRule",
        "rate(1 hour)",
        {"source": "fde.gate.expiry"},
        "Expire proposals past their gate SLA",
    ),
)


def provider_api_key_secret(scope: Construct, params: LaunchParams) -> secretsmanager.Secret:
    """The ONE Secrets Manager secret both `GateService` (`FDE_MODEL_API_KEY`
    below) and `Services`' MCP/embedder containers (`FDE_EMBED_API_KEY`,
    `services.py`) read from -- the brief's "ONE new Secrets Manager
    secret" for the `ProviderApiKey` launch parameter. Callers turn the
    returned secret into an env var value with `dynamic_secret_env_value`
    (`params.py`).

    Built once, by `stack.py`, and passed into both constructs as an
    object -- not built separately inside each -- because `Services` is
    constructed before `GateService` (it needs to exist first so
    `GateService` can read `services.mcp_url`) and a secret built twice
    would be two different `AWS::SecretsManager::Secret` resources holding
    the same value, not one shared source of truth.

    v1 does not have a separate embed-provider API key parameter (`params.
    py`'s `LaunchParams` has only `provider_api_key`, no `embed_api_key`)
    -- one launch parameter, one secret, covers both the model provider and
    the embedding provider's API key when either is a non-bedrock,
    key-authenticated provider. This is a deliberate v1 simplification
    (documented in the Task 6 report), not an oversight: splitting it would
    mean adding a whole second `CfnParameter`/`CfnCondition` pair to the
    click surface for a case (different keys for the model vs. embedding
    provider) v1's own `params.py` doesn't otherwise distinguish.

    The `Condition: HasProviderKey` on the underlying `CfnSecret` (set via
    the L1 escape hatch, `cfn_options.condition`, matching `database.py`'s
    own use of `Fn::If` for tier-switching) means the secret resource does
    not exist in the deployed stack at all when a launcher left
    `ProviderApiKey` blank -- not an empty-valued secret sitting around
    unused. `secret_string_value=cdk.SecretValue.cfn_parameter(...)` (not
    `unsafe_plain_text`) is CDK's own purpose-built API for "the secret's
    initial value comes from a `CfnParameter`", which keeps
    `ProviderApiKey`'s `NoEcho=True` guarantee intact end to end.
    """
    secret = secretsmanager.Secret(
        scope,
        "ProviderApiKeySecret",
        description=(
            "Mirrors the ProviderApiKey launch parameter so GateService and "
            "Services can read it via a {{resolve:secretsmanager:...}} "
            "dynamic reference instead of a plaintext env var."
        ),
        secret_string_value=cdk.SecretValue.cfn_parameter(params.provider_api_key),
    )
    cfn_secret = secret.node.default_child
    assert cfn_secret is not None
    cfn_secret.cfn_options.condition = params.has_provider_key
    return secret


class GateService(Construct):
    """`function`: the VPC-attached `fde-gate-service` Lambda (python3.12,
    arm64, 900s/512MB, `function_name=GATE_FUNCTION_NAME` verbatim -- see
    `iam_roles.py`'s own comment on why that literal, not a generated name,
    is load-bearing for `gate_role`'s log-group/self-invoke statements).
    `http_api`: the `apigatewayv2.HttpApi` front door (JWT-authorized
    `ANY /{proxy+}`, unauthenticated `GET /healthz`). Two EventBridge rules
    (`fde-gate-tick`, `fde-gate-expiry`) round it out."""

    function: lambda_.Function
    http_api: apigwv2.HttpApi
    api_endpoint: str

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        params: LaunchParams,
        vpc: ec2.IVpc,
        db_cluster: rds.DatabaseCluster,
        gate_role: iam.Role,
        identity: Identity,
        mcp_url: str,
        provider_api_key_secret: secretsmanager.ISecret,
        runtime_arns: dict[str, str] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        # --- Supplemental read grant (see module docstring) ---
        gate_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ReadGateLoginSecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[fde_db_secrets_wildcard_arn()],
            )
        )
        gate_db_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "GateDbSecretRef", _GATE_DB_SECRET_NAME
        )

        # --- Code location: same Fn::If bucket resolution as Migrations,
        # different release key (fde-gate.zip, not migration-runner.zip) ---
        code_bucket = s3.Bucket.from_bucket_name(
            self, "AssetsBucketRef", resolve_assets_bucket_name(params)
        )
        code_key = f"releases/{params.release_tag.value_as_string}/fde-gate.zip"

        # FDE_MODEL_ID: "ignored for bedrock (per-agent defaults apply)"
        # per params.py's own ModelId description -- Fn::If(IsBedrockModel,
        # "", ModelId) enforces that at the env-var level (blank even if a
        # launcher set ModelId while ModelProvider=bedrock), rather than
        # trusting every future consumer to re-implement the same
        # precedence rule. This is also IsBedrockModel's first consumer
        # (see infra/cdk/README.md's W8001 ledger) and ModelId's only one.
        model_id_env = cdk.Token.as_string(
            cdk.Fn.condition_if(
                params.is_bedrock_model.logical_id, "", params.model_id.value_as_string
            )
        )

        environment: dict[str, str] = {
            "FDE_DB_SECRET_ARN": gate_db_secret.secret_arn,
            "FDE_GATE_FUNCTION_NAME": GATE_FUNCTION_NAME,
            "FDE_SERVICE_NAME": "fde-gate",
            "FDE_MCP_URL": mcp_url,
            "FDE_MODEL_PROVIDER": params.model_provider.value_as_string,
            "FDE_MODEL_ID": model_id_env,
            # CompatBaseUrl is the one base-URL launch parameter v1 has --
            # shared between the model provider and (services.py) the
            # embedding provider, same simplification as the API key
            # secret above.
            "FDE_MODEL_BASE_URL": params.compat_base_url.value_as_string,
            "FDE_MODEL_API_KEY": dynamic_secret_env_value(provider_api_key_secret, params),
        }
        # Placeholder shape until Task 7's Agents construct exists (see
        # module docstring): every FDE_RUNTIME_ARN_* key is always present,
        # "" until a real runtime_arns dict supplies it, so this task's
        # Lambda environment shape is stable and testable without Agents.
        resolved_runtime_arns = runtime_arns or {}
        for agent_name in _AGENT_NAMES:
            environment[f"FDE_RUNTIME_ARN_{agent_name}"] = resolved_runtime_arns.get(agent_name, "")

        self.function = lambda_.Function(
            self,
            "Function",
            function_name=GATE_FUNCTION_NAME,
            description="FDE gate service: review queue, merge, workflow runner, console",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler=_HANDLER_ENTRYPOINT,
            code=lambda_.Code.from_bucket(code_bucket, code_key),
            timeout=_TIMEOUT,
            memory_size=_MEMORY_MB,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            role=gate_role,
            environment=environment,
        )

        # Same pattern as Migrations: open 5432 from this Lambda's own
        # (auto-created, since no security_groups= override is passed)
        # security group via the cluster's own default-port helper.
        db_cluster.connections.allow_default_port_from(
            self.function, "fde-gate-service Lambda connects to Postgres"
        )

        # --- HTTP API: JWT authorizer on ANY /{proxy+}, none on /healthz
        # (api.py's own two-route contract -- see that file's module
        # docstring for why not one route per gate endpoint) ---
        # Issuer shape matches api.py's own --jwt-issuer help text
        # ("https://cognito-idp.<region>.amazonaws.com/<user-pool-id>") --
        # NOT identity.discovery_url, which carries the
        # /.well-known/openid-configuration suffix a JWT issuer string must
        # not have.
        jwt_issuer = (
            f"https://cognito-idp.{cdk.Aws.REGION}.amazonaws.com/{identity.user_pool.user_pool_id}"
        )
        authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "GateJwtAuthorizer",
            jwt_issuer,
            jwt_audience=[identity.api_client.user_pool_client_id],
        )
        integration = apigwv2_integrations.HttpLambdaIntegration("GateIntegration", self.function)

        self.http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name="fde-gate-api",
            description="FDE gate service: review API and prod-ops console",
            # No cors_preflight=: api.py's own comment is the reason --
            # the console is same-origin and every other client is
            # server-side, so a permissive CORS policy would only weaken
            # the one thing standing between a reviewer's browser session
            # and the rest of the internet.
        )
        self.http_api.add_routes(
            path="/{proxy+}",
            methods=[apigwv2.HttpMethod.ANY],
            integration=integration,
            authorizer=authorizer,
        )
        self.http_api.add_routes(
            path="/healthz",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
            # No authorizer= -- api.py's own reasoning: "a health check
            # that needs a valid JWT is a health check nothing can call."
        )
        self.api_endpoint = self.http_api.api_endpoint

        # --- EventBridge schedules ---
        for logical_id, schedule_expr, payload, description in _SCHEDULES:
            rule = events.Rule(
                self,
                logical_id,
                schedule=events.Schedule.expression(schedule_expr),
                description=description,
            )
            rule.add_target(
                events_targets.LambdaFunction(
                    self.function, event=events.RuleTargetInput.from_object(payload)
                )
            )
