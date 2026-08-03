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

# A pragmatic (not RFC-5322-exhaustive) email shape: local@domain.tld.
# Good enough to reject obvious garbage in the CloudFormation console before
# it reaches Cognito/the gate principal seed.
_EMAIL_ALLOWED_PATTERN = r"[^\s@]+@[^\s@]+\.[^\s@]+"

# v1 ships one region only (see plan: "v1 region: us-east-1 only -- assets
# bucket must be same-region as Lambda code"). release.yml sets
# FDE_ASSETS_BUCKET at synth time once the real bucket exists; the fallback
# below is only ever seen in local/dev synths.
_DEFAULT_ASSETS_BUCKET = "fde-platform-assets-us-east-1"


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

    is_production: cdk.CfnCondition
    is_bedrock_model: cdk.CfnCondition
    has_provider_key: cdk.CfnCondition
    has_assets_bucket: cdk.CfnCondition

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
        is_production=is_production,
        is_bedrock_model=is_bedrock_model,
        has_provider_key=has_provider_key,
        has_assets_bucket=has_assets_bucket,
        assets_region_map=assets_region_map,
    )
