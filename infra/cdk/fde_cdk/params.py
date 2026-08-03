"""The click surface: every CfnParameter, CfnCondition, and CfnMapping a
person launching the stack from the CloudFormation console interacts with.

Task 3-7 constructs take the `LaunchParams` this module builds and read off
it -- nothing else in the stack should call `cdk.CfnParameter` directly, so
the full parameter contract stays in one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import aws_cdk as cdk
import aws_cdk.aws_secretsmanager as secretsmanager

# A pragmatic (not RFC-5322-exhaustive) email shape: local@domain.tld.
# Good enough to reject obvious garbage in the CloudFormation console before
# it reaches Cognito/the gate principal seed.
_EMAIL_ALLOWED_PATTERN = r"[^\s@]+@[^\s@]+\.[^\s@]+"

# v1 ships one region only (see plan: "v1 region: us-east-1 only -- assets
# bucket must be same-region as Lambda code"). release.yml sets
# FDE_ASSETS_BUCKET at synth time once the real bucket exists; the fallback
# below is only ever seen in local/dev synths.
_DEFAULT_ASSETS_BUCKET = "fde-platform-assets-us-east-1"

# Task 6: the ECR Public repository alias `Services`/`GateService`'s
# container images live under (`{ECR_PUBLIC_BASE}/fde-mcp:{ReleaseTag}`).
# `FDE_ECR_PUBLIC_ALIAS` overrides it at synth time the same way
# `FDE_ASSETS_BUCKET` overrides `_DEFAULT_ASSETS_BUCKET` above -- release.yml
# sets it once the platform's real ECR Public alias exists; local/dev synths
# fall back to a value that is obviously a placeholder rather than
# something that looks like it might work. This is a plain synth-time
# constant, not a `CfnMapping` like `AssetsRegionMap`: ECR Public
# (`public.ecr.aws`) is one global namespace, not a per-`AWS::Region`
# resource, so there is no per-region value for a mapping to hold -- a
# `CfnMapping` keyed by `AWS::Region` here would have exactly one branch,
# used unconditionally, which is just a constant with extra ceremony.
#
# Task 7's `Agents` construct needs the bare alias (not the
# `public.ecr.aws/`-prefixed form below): its runtime container URIs go
# through the ECR-Public pull-through cache
# (`{account}.dkr.ecr.{region}.amazonaws.com/ecr-public/{alias}/fde-{agent}:
# {tag}`), which is a *private* ECR registry path, not `public.ecr.aws/...`.
# `ECR_PUBLIC_BASE` below is built from this constant (not a second,
# independent `os.environ.get` call) so the two can never read a different
# alias from the same process.
# Lowercase (Task 7 fix): ECR repository-path segments are lowercase-only
# (`[a-z0-9]+(?:[._-][a-z0-9]+)*`) -- `cfn-lint` only started enforcing this
# for the alias once `agents.py` gave it a consumer with a strict pattern
# (`CfnRuntime.AgentRuntimeArtifact.ContainerConfiguration.ContainerUri`;
# `services.py`'s own `ECR_PUBLIC_BASE` consumer, `ecs.ContainerImage.
# from_registry`, has no such schema constraint, so the all-caps literal
# never surfaced a lint failure through Task 6). Still an obvious,
# not-a-real-alias placeholder -- just one that is syntactically valid so a
# local/dev bare synth (no `FDE_ECR_PUBLIC_ALIAS`) stays cfn-lint-clean.
ECR_PUBLIC_ALIAS = os.environ.get("FDE_ECR_PUBLIC_ALIAS", "replace-at-release")
ECR_PUBLIC_BASE = f"public.ecr.aws/{ECR_PUBLIC_ALIAS}"


@dataclass(frozen=True)
class LaunchParams:
    """Every `cdk.CfnParameter` / `cdk.CfnCondition` / `cdk.CfnMapping`
    `add_launch_params` creates on a stack, bundled for later constructs."""

    deploy_tier: cdk.CfnParameter
    model_provider: cdk.CfnParameter
    model_id: cdk.CfnParameter
    provider_api_key: cdk.CfnParameter
    compat_base_url: cdk.CfnParameter
    embed_provider: cdk.CfnParameter
    embed_model_id: cdk.CfnParameter
    admin_email: cdk.CfnParameter
    release_tag: cdk.CfnParameter
    assets_bucket: cdk.CfnParameter
    # Task 7.5: the ops-layer click surface. See OpsLayer (ops.py) for what
    # each condition below gates.
    ops_mode: cdk.CfnParameter
    ops_alert_email: cdk.CfnParameter
    monthly_budget_usd: cdk.CfnParameter

    is_production: cdk.CfnCondition
    is_bedrock_model: cdk.CfnCondition
    has_provider_key: cdk.CfnCondition
    has_assets_bucket: cdk.CfnCondition
    # OpsEnabled: OpsMode != "off" -- gates every resource OpsLayer creates
    # (params.py's own charter: every CfnCondition lives here, not inside
    # the construct that consumes it -- same reason is_production/
    # is_bedrock_model/has_provider_key/has_assets_bucket all live here).
    ops_enabled: cdk.CfnCondition
    # OpsEmailEnabled: OpsMode == "email" -- gates the SNS email
    # subscription specifically (topic-only mode wants the topic without
    # the default email sink).
    ops_email_enabled: cdk.CfnCondition
    # HasOpsAlertEmail: OpsAlertEmail != "" -- the blank-string-sentinel
    # pattern has_provider_key/has_assets_bucket already use, deciding
    # whether the email subscription's address resolves to OpsAlertEmail or
    # falls back to AdminEmail (see ops_alert_email_value below).
    has_ops_alert_email: cdk.CfnCondition
    # OpsBudgetEnabled: OpsEnabled AND MonthlyBudgetUsd != 0 -- 0 is the
    # documented "no budget" sentinel (params.py's own MonthlyBudgetUsd
    # description).
    ops_budget_enabled: cdk.CfnCondition

    assets_region_map: cdk.CfnMapping


def add_launch_params(stack: cdk.Stack) -> LaunchParams:
    """Create every launch-time parameter, condition, and region mapping on
    `stack` and return them bundled as a `LaunchParams`. Call once per
    stack -- `FdePlatformStack.__init__` does this as `self.params =
    add_launch_params(self)` before any construct that reads them.
    """
    deploy_tier = cdk.CfnParameter(
        stack,
        "DeployTier",
        type="String",
        default="demo",
        allowed_values=["demo", "production"],
        description=(
            "demo = Aurora Serverless v2 (cheap, min-ACU floor for HNSW); "
            "production = provisioned db.r6g.xlarge with deletion protection."
        ),
    )
    model_provider = cdk.CfnParameter(
        stack,
        "ModelProvider",
        type="String",
        default="bedrock",
        allowed_values=["bedrock", "anthropic", "openai", "gemini", "openai-compat"],
        description="LLM provider the three agents call -- the multi-provider seam.",
    )
    model_id = cdk.CfnParameter(
        stack,
        "ModelId",
        type="String",
        default="",
        description=(
            "Required for non-bedrock providers; ignored for bedrock (per-agent defaults apply)."
        ),
    )
    provider_api_key = cdk.CfnParameter(
        stack,
        "ProviderApiKey",
        type="String",
        default="",
        no_echo=True,
        description=(
            "API key for a non-bedrock provider. Stored in Secrets Manager, "
            "never echoed back. Blank is valid for bedrock."
        ),
    )
    compat_base_url = cdk.CfnParameter(
        stack,
        "CompatBaseUrl",
        type="String",
        default="",
        description="Base URL for the openai-compat provider only.",
    )
    embed_provider = cdk.CfnParameter(
        stack,
        "EmbedProvider",
        type="String",
        default="bedrock",
        allowed_values=["bedrock", "openai", "gemini", "openai-compat"],
        description="Embedding provider -- same seam as ModelProvider, chosen independently.",
    )
    embed_model_id = cdk.CfnParameter(
        stack,
        "EmbedModelId",
        type="String",
        default="amazon.titan-embed-text-v2:0",
        description="Embedding model id.",
    )
    admin_email = cdk.CfnParameter(
        stack,
        "AdminEmail",
        type="String",
        allowed_pattern=_EMAIL_ALLOWED_PATTERN,
        constraint_description="must be a valid email address",
        description=(
            "Seeds the Cognito admin user and the gate principal so the "
            "review console works on first login. Required -- no default."
        ),
    )
    release_tag = cdk.CfnParameter(
        stack,
        "ReleaseTag",
        type="String",
        # release.yml sets FDE_RELEASE_TAG before synthesizing the template
        # it uploads, so the launch template's own default already points
        # at the artifacts release.yml just built. Local/dev synths (no env
        # var) fall back to "dev".
        default=os.environ.get("FDE_RELEASE_TAG", "dev"),
        description="Selects the ECR Public image set and artifact zips for this launch.",
    )
    assets_bucket = cdk.CfnParameter(
        stack,
        "AssetsBucket",
        type="String",
        default="",
        description=(
            "S3 bucket holding release artifacts (Lambda zips, migration SQL). "
            "Leave blank to use the region default from AssetsRegionMap."
        ),
    )

    # --- Task 7.5: ops layer click surface ---
    # "email" is the default because the whole point of Part B is that a
    # launcher gets a working alarm->inbox path with zero extra
    # configuration; "topic-only" and "off" are the explicit opt-outs for
    # someone who already has their own ops stack (see the module docstring
    # in ops.py -- the SNS topic is the seam that stays even in "topic-only").
    ops_mode = cdk.CfnParameter(
        stack,
        "OpsMode",
        type="String",
        default="email",
        allowed_values=["email", "topic-only", "off"],
        description=(
            "email = SNS topic + email subscription + dashboard + budget "
            "(default). topic-only = SNS topic only -- subscribe your own "
            "Datadog/PagerDuty/SIEM, skip the default email. off = no ops "
            "resources at all (bring your own health/alarm stack)."
        ),
    )
    ops_alert_email = cdk.CfnParameter(
        stack,
        "OpsAlertEmail",
        type="String",
        default="",
        description=(
            "Address the ops SNS topic emails alarms/budget notifications to "
            "when OpsMode=email. Blank falls back to AdminEmail."
        ),
    )
    monthly_budget_usd = cdk.CfnParameter(
        stack,
        "MonthlyBudgetUsd",
        type="Number",
        default=300,
        min_value=0,
        description=(
            "AWS Budgets monthly cost alert threshold in USD, notified to the "
            "same ops email. 0 disables the budget entirely (no AWS::Budgets::"
            "Budget resource, regardless of OpsMode)."
        ),
    )

    is_production = cdk.CfnCondition(
        stack,
        "IsProduction",
        expression=cdk.Fn.condition_equals(deploy_tier.value_as_string, "production"),
    )
    is_bedrock_model = cdk.CfnCondition(
        stack,
        "IsBedrockModel",
        expression=cdk.Fn.condition_equals(model_provider.value_as_string, "bedrock"),
    )
    has_provider_key = cdk.CfnCondition(
        stack,
        "HasProviderKey",
        expression=cdk.Fn.condition_not(
            cdk.Fn.condition_equals(provider_api_key.value_as_string, "")
        ),
    )
    # Consumed by Task 5's `Migrations` construct: `Fn::If(HasAssetsBucket,
    # AssetsBucket, FindInMap(AssetsRegionMap, AWS::Region, "bucket"))`
    # picks the migration-runner Lambda's code bucket -- the override param
    # when a launcher set one, else the region default. Same blank-string
    # sentinel pattern as `has_provider_key` above.
    has_assets_bucket = cdk.CfnCondition(
        stack,
        "HasAssetsBucket",
        expression=cdk.Fn.condition_not(cdk.Fn.condition_equals(assets_bucket.value_as_string, "")),
    )

    # --- Task 7.5: ops layer conditions ---
    ops_enabled = cdk.CfnCondition(
        stack,
        "OpsEnabled",
        expression=cdk.Fn.condition_not(cdk.Fn.condition_equals(ops_mode.value_as_string, "off")),
    )
    ops_email_enabled = cdk.CfnCondition(
        stack,
        "OpsEmailEnabled",
        expression=cdk.Fn.condition_equals(ops_mode.value_as_string, "email"),
    )
    has_ops_alert_email = cdk.CfnCondition(
        stack,
        "HasOpsAlertEmail",
        expression=cdk.Fn.condition_not(
            cdk.Fn.condition_equals(ops_alert_email.value_as_string, "")
        ),
    )
    # `Fn::And` referencing the already-named OpsEnabled condition directly
    # (verified: `Fn.condition_and` accepts a `CfnCondition` object and
    # emits `{"Condition": "OpsEnabled"}`, not a re-expansion of its
    # expression) -- MonthlyBudgetUsd's own description names "0" as the
    # no-budget sentinel, checked as a string equality the same way every
    # other blank/zero sentinel in this module is (Number CfnParameters
    # still expose `.value_as_string`).
    ops_budget_enabled = cdk.CfnCondition(
        stack,
        "OpsBudgetEnabled",
        expression=cdk.Fn.condition_and(
            ops_enabled,
            cdk.Fn.condition_not(cdk.Fn.condition_equals(monthly_budget_usd.value_as_string, "0")),
        ),
    )

    assets_region_map = cdk.CfnMapping(
        stack,
        "AssetsRegionMap",
        mapping={
            "us-east-1": {
                "bucket": os.environ.get("FDE_ASSETS_BUCKET", _DEFAULT_ASSETS_BUCKET),
            },
        },
    )

    return LaunchParams(
        deploy_tier=deploy_tier,
        model_provider=model_provider,
        model_id=model_id,
        provider_api_key=provider_api_key,
        compat_base_url=compat_base_url,
        embed_provider=embed_provider,
        embed_model_id=embed_model_id,
        admin_email=admin_email,
        release_tag=release_tag,
        assets_bucket=assets_bucket,
        ops_mode=ops_mode,
        ops_alert_email=ops_alert_email,
        monthly_budget_usd=monthly_budget_usd,
        is_production=is_production,
        is_bedrock_model=is_bedrock_model,
        has_provider_key=has_provider_key,
        has_assets_bucket=has_assets_bucket,
        ops_enabled=ops_enabled,
        ops_email_enabled=ops_email_enabled,
        has_ops_alert_email=has_ops_alert_email,
        ops_budget_enabled=ops_budget_enabled,
        assets_region_map=assets_region_map,
    )


# ---------------------------------------------------------------------
# Small pure helpers over a `LaunchParams` a later construct needs and
# would otherwise duplicate. None of these create a `CfnParameter`/
# `CfnCondition`/`CfnMapping` themselves (that would violate this module's
# own charter, stated at the top of the file) -- they only read the ones
# `add_launch_params` already built, so the exact `Fn::If`/dynamic-reference
# shape has one source of truth instead of a copy per consumer.
# ---------------------------------------------------------------------


def resolve_assets_bucket_name(params: LaunchParams) -> str:
    """`Fn::If(HasAssetsBucket, AssetsBucket, FindInMap(AssetsRegionMap,
    AWS::Region, "bucket"))`, wrapped as a plain `str` carrying an embedded
    CDK token (`cdk.Token.as_string`, matching `identity.py`'s
    `discovery_url` -- see that module for why an f-string/token mix is the
    CDK-endorsed way to build a composite string).

    Extracted here (Task 6) from `migrations.py`, which had the only copy
    through Task 5: `gate.py`'s Lambda code (`releases/{tag}/fde-gate.zip`)
    resolves its S3 bucket exactly the same way `migrations.py`'s Lambda
    code (`releases/{tag}/migration-runner.zip`) does, and a second
    hand-copied `Fn::If`/`Fn::FindInMap` pair would be one more place a
    future change to the resolution rule could drift from the other.
    `migrations.py` was updated in the same commit to call this instead of
    inlining its own copy.
    """
    return cdk.Token.as_string(
        cdk.Fn.condition_if(
            params.has_assets_bucket.logical_id,
            params.assets_bucket.value_as_string,
            cdk.Fn.find_in_map(params.assets_region_map.logical_id, cdk.Aws.REGION, "bucket"),
        )
    )


def fde_db_secrets_wildcard_arn() -> str:
    """`arn:aws:secretsmanager:{region}:{account}:secret:fde/db/*` -- the
    three migration-minted login secrets (`fde/db/agent`, `fde/db/gate`,
    `fde/db/ingest`; see `infra/cdk/lambdas/migration_runner/handler.py`'s
    `LOGIN_SECRETS`).

    `IamRoles.migration_role` already grants exactly this pattern (it
    *mints* the three secrets -- see `iam_roles.py`'s `MintLoginUserSecrets`
    statement). Every later task's compute that only *reads* one of the
    three -- `GateService`'s supplemental grant on `gate_role` (see that
    module's docstring for why `gate_role`'s own IAM template does not
    already cover this), `Services`' two Fargate task roles -- reuses the
    identical wildcard rather than a narrower per-secret ARN, both for
    consistency with the migration role's own grant and because each
    secret's real ARN carries a Secrets-Manager-generated 6-character
    suffix that is not knowable at synth time (`Secret.from_secret_name_v2`
    is used separately, for the env var *value*, precisely because it
    tolerates that -- see `gate.py`).
    """
    return cdk.Fn.join(
        "",
        [
            "arn:aws:secretsmanager:",
            cdk.Aws.REGION,
            ":",
            cdk.Aws.ACCOUNT_ID,
            ":secret:fde/db/*",
        ],
    )


def dynamic_secret_env_value(secret: secretsmanager.ISecret, params: LaunchParams) -> str:
    """`Fn::If(HasProviderKey, {{resolve:secretsmanager:<secret-arn>:
    SecretString}}, "")` -- an env var value that resolves to `secret`'s
    live content at CloudFormation deploy time (never baked into the
    template as plaintext) when a launcher supplied a `ProviderApiKey`, and
    to an empty string when they did not.

    Built from CDK's own `SecretValue.secrets_manager(...).unsafe_unwrap()`
    (not a hand-rolled `Fn::Join` of the `{{resolve:...}}` literal) --
    `unsafe_unwrap()` is CDK's documented escape hatch for exactly this
    case (a secret's value flowing into a property, like a Lambda/container
    environment variable, that legitimately needs the raw dynamic-reference
    string rather than a `SecretValue` wrapper). Verified via a standalone
    synth (not against live AWS -- see the repo's AWS honesty rule) that
    this produces `Fn::Join(["", ["{{resolve:secretsmanager:", {Ref:
    <secret logical id>}, ":SecretString:::}}"]])`, which CloudFormation
    resolves at deploy time the same way it would a literal `{{resolve:
    ...}}` string used directly in a property value.

    The `Fn::If` wrap matters independently of the secret's own content:
    `secret`'s underlying `AWS::SecretsManager::Secret` resource (built by
    `gate.py`'s `provider_api_key_secret`) is itself conditioned on
    `HasProviderKey` (`Condition: HasProviderKey` on the resource, not just
    on this value) -- when a launcher left `ProviderApiKey` blank, that
    resource never exists in the deployed stack at all, so a dynamic
    reference to it must never be *evaluated* by CloudFormation, only
    *present* in the template's unused `Fn::If` branch. CloudFormation
    does not evaluate the untaken branch of `Fn::If`, which is exactly what
    makes referencing a conditionally-absent resource there safe (the same
    pattern `database.py` and AWS's own docs use for conditionally-created
    resources).
    """
    dynamic_ref = cdk.SecretValue.secrets_manager(secret.secret_arn).unsafe_unwrap()
    return cdk.Token.as_string(
        cdk.Fn.condition_if(params.has_provider_key.logical_id, dynamic_ref, "")
    )


def model_id_env(params: LaunchParams) -> str:
    """`Fn::If(IsBedrockModel, "", ModelId)` -- the `FDE_MODEL_ID` env value
    shared by every process that authors an agent turn. `params.py`'s own
    `ModelId` description is the rule this encodes: "ignored for bedrock
    (per-agent defaults apply)" -- a launcher who left `ModelProvider` at
    its `bedrock` default but still typed something into `ModelId` must not
    have that value leak into the environment, because the per-agent
    resolution `fde_agents.common.config.resolve_model_id`/`MODEL_PRESETS`
    already picks a concrete Bedrock model id at runtime.

    Extracted here (Task 7) from `gate.py`, which had the only copy through
    Task 6, so `Agents`' three runtimes (Task 7's actual, audit-relevant
    consumer -- see `agents.py`'s module docstring for why the gate
    Lambda's own copy of this value is inert pass-through, not what an
    agent process reads) and `GateService`'s Lambda env compute the exact
    same `Fn::If` rather than two hand-copied literals that could drift
    apart. `gate.py` was updated in the same commit to call this instead of
    inlining its own copy.
    """
    return cdk.Token.as_string(
        cdk.Fn.condition_if(params.is_bedrock_model.logical_id, "", params.model_id.value_as_string)
    )


def ops_alert_email_value(params: LaunchParams) -> str:
    """`Fn::If(HasOpsAlertEmail, OpsAlertEmail, AdminEmail)` -- the address
    `OpsLayer`'s (`ops.py`) SNS email subscription resolves to: the
    `OpsAlertEmail` launch parameter when a launcher set one, else the
    `AdminEmail` every launch already requires (so there is always a real
    inbox to fall back to, never a blank subscription endpoint).

    Same shape as `resolve_assets_bucket_name`/`dynamic_secret_env_value`
    above: a plain `str` carrying an embedded CDK token via
    `cdk.Token.as_string`. This resolves the value regardless of `OpsMode`
    -- `ops.py` is the one that decides whether the *subscription resource*
    consuming it exists at all (`Condition: OpsEmailEnabled`), matching the
    same separation `dynamic_secret_env_value` draws between "what does
    this value resolve to" and "does the resource holding it exist".
    """
    return cdk.Token.as_string(
        cdk.Fn.condition_if(
            params.has_ops_alert_email.logical_id,
            params.ops_alert_email.value_as_string,
            params.admin_email.value_as_string,
        )
    )
