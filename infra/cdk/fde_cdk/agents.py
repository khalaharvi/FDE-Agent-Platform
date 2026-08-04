"""`Agents`: the ECR-Public pull-through cache rule, the AgentCore Gateway
fronting the FDE knowledge-graph MCP server, the AgentCore Memory resource,
and the three AgentCore Runtimes (Engagement, Workflow, Development).

Every AgentCore resource here is an L1 `Cfn*` (`aws_cdk.aws_bedrockagentcore`)
-- not a "prefer L2, fall back to L1 if it fights you" choice: this CDK
version ships NO L2 for `aws_bedrockagentcore` at all (`infra/cdk/README.md`'s
own `dir(bac)` dump lists only `Cfn*`/`Cfn*Props` names). `ecr.
CfnPullThroughCacheRule` is likewise the only shape for a pull-through cache
rule; `aws_ecr` has no L2 wrapper for it either.

Property names were cross-checked two ways, per this task's own instructions
-- never guessed:
  1. Local introspection of the INSTALLED library (`aws-cdk-lib==2.263.0`,
     the version `infra/cdk/README.md` pins): `inspect.signature(...)` on
     `CfnRuntime`/`CfnGateway`/`CfnMemory`/`CfnOAuth2CredentialProvider` and
     every `*Property` type this module constructs. This is authoritative
     -- it is literally the code that will run.
  2. Context7 (`aws_cdk.aws_bedrockagentcore`, AWS CDK Python Reference)
     as a second, independent source. Agreed exactly with (1) on every
     field name used below (`agentRuntimeArtifact`/`networkConfiguration`/
     `authorizerConfiguration`/`customJwtAuthorizer`/`discoveryUrl`/
     `allowedAudience`, `protocolConfiguration.mcp.searchType`, etc.).
  3. Cross-checked a third time against this repo's own boto3 payloads --
     `packages/fde-agents/src/fde_agents/deploy/runtimes.py`'s
     `create_runtime` (:131), `_build_artifact` (:59), `_environment_
     variables` (:117), `_authorizer_configuration` (:85); `gateway.py`'s
     `create_gateway` (:83); `memory.py`'s `create_memory` (:148) plus its
     three `_*_strategy` builders (:76-117) -- the CFN property names are
     the same words as the boto3 kwargs, just re-cased (`containerUri` ->
     `container_uri`, etc.), confirming the CFN resource types are a
     direct mirror of the control-plane API these scripts already call by
     hand.
  4. For the v0.3.0-rc4 network-mode pivot specifically (see "As-built
     network architecture (v1)" below), the installed library's
     introspected shape was additionally checked against the LIVE
     CloudFormation registry schema itself (`aws cloudformation
     describe-type --type RESOURCE --type-name
     AWS::BedrockAgentCore::Runtime`) rather than trusting cfn-lint's
     bundled copy, which had already been shown wrong once this same rc
     (the `GatewayTarget` endpoint pattern below).

Construction order inside this construct: pull-through cache rule -> Gateway
-> credential provider -> Memory -> Runtimes. There is no longer any
Gateway-before-Runtimes ordering *constraint* (an earlier revision of this
module threaded `self.gateway_url` into every runtime's `FDE_GATEWAY_URL`
env and needed the Gateway built first to satisfy that CloudFormation
dependency) -- the pivot described below disconnects the Gateway from the
Runtimes entirely. Gateway is still built first only because it reads more
naturally next to the pull-through cache rule; nothing depends on the order.

`self.gateway_url`
-------------------
`CfnGateway.attr_gateway_url` IS exposed as a CloudFormation attribute in
this lib version (confirmed via introspection: `'attr_gateway_url' in
dir(CfnGateway)`) and is still captured on `self.gateway_url` below. As of
the v0.3.0-rc4 pivot it is no longer threaded into any runtime's
environment (see below) -- it is kept as a construct attribute only so a
future v2 wire-up (an HTTPS-fronted Gateway target) has the value on hand
without re-deriving it.

As-built network architecture (v1) -- supersedes the old "Known
launch-readiness gap" note
---------------------------------------------------------------------------
The v0.3.0-rc4 live-launch attempt hit a real CloudFormation validation
failure, not a hypothetical one flagged for a future live test:
`AWS::BedrockAgentCore::GatewayTarget`'s LIVE registry schema (`aws
cloudformation describe-type --type RESOURCE --type-name
AWS::BedrockAgentCore::GatewayTarget`) requires
`McpServerTargetConfiguration.Endpoint` to match `^https://.*`, and this
stack's only MCP endpoint was `services.mcp_url` -- `services.py`'s
INTERNAL ALB, plain HTTP, no TLS listener, `internet_facing=False`. The
repo owner's decision, given that finding: **no publicly exposed MCP
gateway, period** -- standing up an HTTPS front for an internal ALB just to
satisfy a Gateway target's regex was rejected outright, not attempted.

What changed here, replacing the old three-surface "unresolved, needs a
live test" gap with an as-built resolution:

  1. **The Gateway target is gone.** No `CfnGatewayTarget` is built by this
     construct any more. `CfnGateway` and `CfnOAuth2CredentialProvider`
     both stay -- each provisions cleanly on its own, with no dependency on
     an MCP endpoint -- but the Gateway is now provisioned-but-UNWIRED: no
     target points at `services.mcp_url` through it, and no runtime is
     configured to call it (point 3, below). Wiring a real target is v2
     scope, gated on the owner's no-public-MCP-gateway decision changing.
  2. **Runtimes join the VPC.** Every `CfnRuntime.NetworkConfiguration`
     below is `network_mode="VPC"` -- the live registry schema's
     `NetworkMode` enum is `["PUBLIC", "VPC"]` (confirmed via the same
     `describe-type` query as above, not cfn-lint's bundled copy, which is
     exactly what mis-described the GatewayTarget pattern in the first
     place) -- attached to `vpc`'s private-with-egress subnets through one
     dedicated `runtime_security_group` (egress-all; DB ingress opened
     one-directionally via `db_cluster.connections.
     allow_default_port_from(runtime_security_group, ...)`, the same idiom
     `Migrations`/`GateService`/`Services` already use). This resolves the
     old surfaces 2 and 3 directly: both `FDE_DB_SECRET_ARN` (tracing
     writes) and the in-container stdio MCP subprocess (point 3) now have
     a private route to Aurora.
  3. **In-container stdio MCP, not Gateway, for every runtime.**
     `FDE_GATEWAY_URL`/`FDE_GATEWAY_SCOPES` are no longer set on any
     runtime's environment. `GatewaySettings.url`'s absence is exactly
     what selects `mcp_tools.build_mcp_client`'s stdio transport
     (`fde_agents/common/config.py`): each runtime now spawns its own
     in-container `python -m fde_mcp` subprocess and talks to it over
     stdio -- the same dev-mode code path an earlier revision of this
     docstring flagged as "should never trigger in this deployment." It is
     now the ONLY MCP path every deployed runtime takes, by design, not by
     accident.

The old surface 1 (Gateway -> internal ALB reachability) is moot now, not
resolved -- there is no Gateway target left for that question to apply to.
If a v2 change ever stands up an HTTPS-fronted MCP endpoint and wires a
`CfnGatewayTarget` back in, surface 1's original question (can a
non-VPC-attached Gateway reach a VPC-internal HTTPS listener) becomes live
again and needs its own verification then.

The ECR pull-through cache rule
--------------------------------
`ecr.CfnPullThroughCacheRule(ecr_repository_prefix="ecr-public",
upstream_registry_url="public.ecr.aws")` lets a private-ECR pull for
`{account}.dkr.ecr.{region}.amazonaws.com/ecr-public/{alias}/fde-{agent}:
{tag}` transparently fetch-and-cache the upstream `public.ecr.aws/{alias}/
fde-{agent}:{tag}` image the CI release build already publishes (the SAME
image `services.py`'s ECS tasks pull directly from `public.ecr.aws` --
AgentCore Runtime, unlike Fargate, requires its container artifact to live
in a *private* ECR registry, which is the whole reason this rule exists).
Each runtime's `container_uri` string is built from plain Python string
interpolation over CDK tokens (`cdk.Aws.ACCOUNT_ID`/`cdk.Aws.REGION`), which
carries no reference to the `CfnPullThroughCacheRule` construct itself -- so
CDK's automatic same-token dependency inference does not apply here (unlike,
e.g., the `network_mode_config`/`db_cluster.connections` case above, where a
real construct reference DOES let CDK infer the dependency automatically).
Each runtime instead gets an explicit
`add_resource_dependency(self.pull_through_cache_rule)` (the current,
non-deprecated form -- `CfnResource.add_dependency` still exists in this
lib version but logs a deprecation warning pointing at this replacement):
the cache rule must exist before CloudFormation attempts to create a
runtime whose artifact resolves through it, even though nothing in the
runtime's own properties textually references the rule.
"""

from __future__ import annotations

import aws_cdk as cdk
import aws_cdk.aws_bedrockagentcore as bedrockagentcore
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_ecr as ecr
import aws_cdk.aws_iam as iam
import aws_cdk.aws_rds as rds
import aws_cdk.aws_secretsmanager as secretsmanager
from constructs import Construct

from fde_cdk.params import (
    ECR_PUBLIC_ALIAS,
    LaunchParams,
    dynamic_secret_env_value,
    fde_db_secrets_wildcard_arn,
    model_id_env,
)

# Matches `fde_agents.deploy.runtimes.AGENT_NAMES` exactly (lowercase --
# the AgentCore API's own `agentRuntimeName`/`FDE_AGENT_NAME` case).
# `gate.py`'s `_AGENT_NAMES` is the uppercase form this module's
# `runtime_arns` dict keys use, matching what `GateSettings.runtime_arn_for`
# looks up.
AGENT_NAMES = ("engagement", "workflow", "development")

# The login secret infra/cdk/lambdas/migration_runner/handler.py's
# LOGIN_SECRETS mints for the `fde_agent` role -- the SAME secret
# `services.py`'s `_MCP_DB_SECRET_NAME` uses for the MCP server container,
# because a runtime's own IAM permissions template
# (`runtime-permissions-policy.json`'s `IamDbAuthFallback` statement,
# scoped to `dbuser:.../fde_agent`) makes explicit that a runtime connects
# to Postgres AS `fde_agent`, the identical DB role the MCP server uses.
_RUNTIME_DB_SECRET_NAME = "fde/db/agent"

# Same ceilings `fde_agents.deploy.runtimes` provisions with by default
# (`DEFAULT_IDLE_TIMEOUT_S`/`DEFAULT_MAX_LIFETIME_S`, that module's own
# docstring: "the verified AgentCore facts this platform is built against").
_IDLE_TIMEOUT_S = 900
_MAX_LIFETIME_S = 28800

# Matches `fde_agents.deploy.memory.DEFAULT_EVENT_EXPIRY_DAYS` -- "long
# enough to span a typical multi-week FDE engagement plus slack for a
# delayed follow-up, short enough that this is clearly NOT meant as a
# durable store (that's the graph's job)" (memory.py's own docstring).
_MEMORY_EVENT_EXPIRY_DAYS = 90

# Verbatim from `fde_agents.deploy.gateway.create_gateway` (:83-100) -- the
# instructions an agent's Gateway-connected MCP client sees describing this
# platform's tool surface.
_GATEWAY_NAME = "fde-kg-gateway"
_GATEWAY_INSTRUCTIONS = (
    "Tools over the FDE knowledge graph: bitemporal process/role/system "
    "graph, hybrid ANN+graph retrieval, human-in-the-loop change "
    "proposals, drift monitoring, and faithful workflow authoring. "
    "Call kg_head_commit first and pin its commit_id for the task."
)
_GATEWAY_SUPPORTED_MCP_VERSIONS = ["2025-06-18"]

# NOT verbatim from memory.py -- a deliberate, documented divergence.
# `memory.py`'s own literals (`fde-agent-memory`, `fde-semantic`,
# `fde-summary`, `fde-user-pref`, and the `{strategyId}` namespace-template
# placeholder) are what that boto3 script has always sent directly to
# `bedrock-agentcore-control.create_memory`, unvalidated against the real
# control plane (the AWS honesty rule -- "written against verified API
# shapes, not validated here"). `cfn-lint` validates against the ACTUAL
# CloudFormation resource-provider JSON schema for
# `AWS::BedrockAgentCore::Memory`, which this task is the first thing in
# the repo to run these literals through, and it rejects every one of
# them: `MemoryStrategyProperty.name`/`CfnMemory.name` must match
# `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` (no hyphens), and a namespace template's
# only valid placeholders are `{actorId}`/`{sessionId}`/
# `{memoryStrategyId}` -- NOT `{strategyId}`. Both are almost certainly
# real bugs in `packages/fde-agents/src/fde_agents/deploy/memory.py` that
# would surface as a `ValidationException` the first time that script ran
# against a live account -- out of scope to fix there in this CDK-only
# task (see this task's report's "concerns" section for the follow-up),
# but reproducing a schema-invalid literal here just to match memory.py
# byte-for-byte would fail this project's OWN cfn-lint gate for no reason
# other than copying a bug. Underscore-only names, `{memoryStrategyId}`.
_MEMORY_NAME = "fde_agent_memory"
_NAMESPACE_TEMPLATE = "/strategy/{memoryStrategyId}/actor/{actorId}/session/{sessionId}"

# I4(c) fix: the AgentCore Identity OAuth2 credential provider name
# `fde_agents.common.config.GatewaySettings.identity_provider_name`
# defaults to when `FDE_IDENTITY_PROVIDER_NAME` is unset -- this construct
# provisions a real `CfnOAuth2CredentialProvider` under this exact name so
# `IdentityClient.get_token(provider_name="fde-gateway-m2m", ...)`
# (`fde_agents/common/mcp_tools.py`'s `_mint_gateway_bearer_token`) finds
# it without any manual, out-of-band provisioning step.
_GATEWAY_M2M_CREDENTIAL_PROVIDER_NAME = "fde-gateway-m2m"


class Agents(Construct):
    """`pull_through_cache_rule`: the ECR-Public pull-through cache rule
    every runtime's container artifact resolves through. `gateway`:
    the AgentCore Gateway, CUSTOM_JWT-authorized against Cognito --
    provisioned but UNWIRED in v1 (see module docstring's "As-built network
    architecture (v1)": no `CfnGatewayTarget` is built here, by owner
    decision, and no runtime calls it). `memory`: the always-on semantic/
    summary/user-preference AgentCore Memory resource. `runtime_security_
    group`: the one shared security group every runtime's VPC network
    configuration uses (egress-all; DB ingress opened from it in this
    constructor). `runtimes`: the three AgentCore Runtimes, keyed by
    lowercase agent name. `runtime_arns`: the same three runtimes' ARNs,
    keyed by the UPPERCASE name `gate.py`'s `FDE_RUNTIME_ARN_{AGENT}` envs
    expect."""

    pull_through_cache_rule: ecr.CfnPullThroughCacheRule
    gateway: bedrockagentcore.CfnGateway
    gateway_url: str
    memory: bedrockagentcore.CfnMemory
    memory_id: str
    memory_arn: str
    runtime_security_group: ec2.SecurityGroup
    runtimes: dict[str, bedrockagentcore.CfnRuntime]
    runtime_arns: dict[str, str]
    # I4(c): the `fde-gateway-m2m` AgentCore Identity OAuth2 credential
    # provider (see module-level `_GATEWAY_M2M_CREDENTIAL_PROVIDER_NAME`).
    gateway_m2m_credential_provider: bedrockagentcore.CfnOAuth2CredentialProvider

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        params: LaunchParams,
        vpc: ec2.IVpc,
        db_cluster: rds.DatabaseCluster,
        runtime_role: iam.Role,
        gateway_service_role: iam.Role,
        memory_role: iam.Role,
        jwt_discovery_url: str,
        jwt_allowed_audience: str,
        m2m_client_secret: cdk.SecretValue,
        provider_api_key_secret: secretsmanager.ISecret,
    ) -> None:
        super().__init__(scope, construct_id)

        # --- ECR-Public pull-through cache rule ---
        # Pinned: `ecr.CfnPullThroughCacheRule` constructor (local
        # introspection, aws-cdk-lib==2.263.0). No `upstream_registry=`
        # (that field selects a named non-ECR-Public upstream like
        # "docker-hub"/"quay"; `upstream_registry_url` is the generic form
        # this brief's `public.ecr.aws` upstream uses).
        self.pull_through_cache_rule = ecr.CfnPullThroughCacheRule(
            self,
            "EcrPublicPullThroughCache",
            ecr_repository_prefix="ecr-public",
            upstream_registry_url="public.ecr.aws",
        )

        # --- Gateway (built before Runtimes -- see module docstring) ---
        # Pinned: `CfnGateway`/`CfnGateway.GatewayProtocolConfiguration
        # Property`/`MCPGatewayConfigurationProperty`/`AuthorizerConfiguration
        # Property`/`CustomJWTAuthorizerConfigurationProperty` (local
        # introspection + context7, both agree). Cross-checked against
        # `gateway.py`'s `create_gateway` (:83-109): `protocolType="MCP"`,
        # `protocolConfiguration.mcp.searchType="SEMANTIC"`,
        # `authorizerType="CUSTOM_JWT"`, same instructions string.
        self.gateway = bedrockagentcore.CfnGateway(
            self,
            "Gateway",
            name=_GATEWAY_NAME,
            description="FDE Platform knowledge-graph MCP Gateway",
            role_arn=gateway_service_role.role_arn,
            protocol_type="MCP",
            protocol_configuration=bedrockagentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=bedrockagentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    search_type="SEMANTIC",
                    supported_versions=_GATEWAY_SUPPORTED_MCP_VERSIONS,
                    instructions=_GATEWAY_INSTRUCTIONS,
                )
            ),
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=bedrockagentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=bedrockagentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=jwt_discovery_url,
                    # Per this task's brief: "allowed audience = m2m client
                    # id". `gateway.py`'s own script takes both
                    # `--jwt-allowed-audience`/`--jwt-allowed-clients` as
                    # independent, operator-supplied lists; this stack has
                    # exactly one M2M caller (`identity.py`'s `m2m_client`),
                    # so its client id is both the only member of this list
                    # here.
                    allowed_audience=[jwt_allowed_audience],
                    # I4(a) fix (final-fix-report.md): `allowed_clients`
                    # checks the token's `client_id` claim, not `aud` --
                    # Cognito's client-credentials grant (what `m2m_client`
                    # uses) mints a token carrying `client_id`, and does
                    # not reliably carry an `aud` claim matching it the way
                    # a user-pool ID token does. Without this, a real M2M
                    # token could fail the CUSTOM_JWT authorizer even
                    # though `allowed_audience` already names the same
                    # client id. Verified via local introspection:
                    # `CustomJWTAuthorizerConfigurationProperty` accepts
                    # `allowed_clients` on this pinned lib version.
                    allowed_clients=[jwt_allowed_audience],
                ),
            ),
        )
        self.gateway_url = self.gateway.attr_gateway_url

        # --- I4(c) fix (final-fix-report.md): the fde-gateway-m2m
        # AgentCore Identity OAuth2 credential provider ---
        # `IdentityClient.get_token(provider_name="fde-gateway-m2m", ...,
        # auth_flow="M2M")` (`fde_agents/common/mcp_tools.py`'s
        # `_mint_gateway_bearer_token`) needs a credential provider by this
        # exact name to already exist -- without it, every deployed
        # runtime's first Gateway call fails outright (no way to mint a
        # bearer token at all). `CfnOAuth2CredentialProvider` exists on
        # this pinned lib version (`infra/cdk/README.md`'s own `dir()`
        # dump) and its shape was cross-checked three ways, matching this
        # module's own established practice for every other AgentCore
        # resource: (1) local introspection of the installed
        # `aws-cdk-lib` (`Oauth2ProviderConfigInputProperty`/
        # `CustomOauth2ProviderConfigInputProperty`/`Oauth2DiscoveryProperty`/
        # `SecretReferenceProperty`); (2) the CDK L2 enum
        # `OAuth2CredentialProviderVendor.COGNITO.value ==
        # "CognitoOauth2"`; (3) the installed `botocore` service model for
        # `bedrock-agentcore-control` (`CredentialProviderVendorType`
        # includes `"CognitoOauth2"`; `Oauth2ProviderConfigInput` is a
        # union with NO dedicated Cognito-specific member -- Cognito, like
        # Okta/Auth0/PingOne, is meant to pair with the generic
        # `customOauth2ProviderConfig` shape, not a bespoke one).
        # `DiscoveryUrlType`'s own pattern
        # (`.+/\.well-known/(openid-configuration|oauth-authorization-
        # server)`) matches `jwt_discovery_url` (`identity.discovery_url`)
        # exactly.
        #
        # The client secret: Cognito does not put a user pool client's
        # generated secret into Secrets Manager on its own, but
        # `CfnUserPoolClient` DOES expose it as a real CloudFormation
        # attribute (`attr_client_secret`, wrapped by the L2 as
        # `UserPoolClient.user_pool_client_secret` -- verified via local
        # introspection) -- retrievable, so this is provisioned for real,
        # not left as a manual step. The value is mirrored into a
        # dedicated Secrets Manager secret (`secret_object_value`, the same
        # `cdk.SecretValue`-carrying L2 API `gate.py`'s
        # `provider_api_key_secret` uses) and referenced via
        # `client_secret_config`/`SecretReferenceProperty`
        # (`client_secret_source="EXTERNAL"`) rather than passed as the
        # plain-string `client_secret` property directly -- the same
        # never-let-a-secret-value-sit-in-a-resource's-own-properties
        # discipline `params.dynamic_secret_env_value` already holds this
        # stack to for `ProviderApiKey`.
        m2m_secret_mirror = secretsmanager.Secret(
            self,
            "GatewayM2mClientSecretMirror",
            description=(
                "Mirrors Identity.m2m_client's Cognito-generated client "
                "secret so the fde-gateway-m2m AgentCore Identity OAuth2 "
                "credential provider can read it via clientSecretConfig "
                "(EXTERNAL) instead of the value sitting directly in that "
                "resource's own CloudFormation properties."
            ),
            secret_object_value={"client_secret": m2m_client_secret},
        )
        self.gateway_m2m_credential_provider = bedrockagentcore.CfnOAuth2CredentialProvider(
            self,
            "GatewayM2mCredentialProvider",
            name=_GATEWAY_M2M_CREDENTIAL_PROVIDER_NAME,
            # LIVE-VALIDATED CORRECTION (v0.3.0-rc1 launch, 2026-08-04): the
            # control plane rejects vendor "CognitoOauth2" paired with
            # `customOauth2ProviderConfig` ("Provided configuration does not
            # match selected type", ValidationException). Vendor and config
            # member must match: the generic custom config pairs with
            # CUSTOM_OAUTH2. Cognito remains the actual issuer -- it is
            # expressed entirely through the discovery URL inside the custom
            # config, exactly as the comment above describes.
            credential_provider_vendor=bedrockagentcore.OAuth2CredentialProviderVendor.CUSTOM.value,
            oauth2_provider_config_input=bedrockagentcore.CfnOAuth2CredentialProvider.Oauth2ProviderConfigInputProperty(
                custom_oauth2_provider_config=bedrockagentcore.CfnOAuth2CredentialProvider.CustomOauth2ProviderConfigInputProperty(
                    oauth_discovery=bedrockagentcore.CfnOAuth2CredentialProvider.Oauth2DiscoveryProperty(
                        discovery_url=jwt_discovery_url,
                    ),
                    client_id=jwt_allowed_audience,
                    client_secret_config=bedrockagentcore.CfnOAuth2CredentialProvider.SecretReferenceProperty(
                        secret_id=m2m_secret_mirror.secret_arn,
                        json_key="client_secret",
                    ),
                    client_secret_source="EXTERNAL",
                )
            ),
        )

        # --- No Gateway target in v1 -- provisioned but UNWIRED ---
        # A `CfnGatewayTarget` used to be built here, pointing at
        # `services.mcp_url` (the internal ALB). Removed by the v0.3.0-rc4
        # pivot: `McpServerTargetConfiguration.Endpoint`'s LIVE registry
        # schema pattern is `^https://.*`, and this stack deliberately has
        # no HTTPS-fronted MCP endpoint (owner decision: no publicly
        # exposed MCP gateway, period -- see module docstring's "As-built
        # network architecture (v1)"). `self.gateway` and
        # `self.gateway_m2m_credential_provider` above are left in place --
        # both provision cleanly with no MCP-endpoint dependency -- for a
        # v2 target to be wired against if that decision ever changes.

        # --- Memory: three always-on strategies, mirroring memory.py's
        # `_semantic_strategy`/`_summary_strategy`/`_user_preference_
        # strategy` (:76-117) verbatim (name, description, namespace
        # template). `customMemoryStrategy`/`episodicMemoryStrategy` are
        # deliberately absent here too, for the exact reasons memory.py's
        # own module docstring gives -- v1 does not add a CDK launch
        # parameter for either. ---
        # Pinned: `CfnMemory`/`MemoryStrategyProperty`/`Semantic
        # MemoryStrategyProperty`/`SummaryMemoryStrategyProperty`/
        # `UserPreferenceMemoryStrategyProperty` (local introspection).
        # Cross-checked against `memory.py`'s `create_memory` (:148-162):
        # `eventExpiryDuration`, `memoryExecutionRoleArn`, `memoryStrategies`
        # as a list of one-key union dicts.
        self.memory = bedrockagentcore.CfnMemory(
            self,
            "Memory",
            name=_MEMORY_NAME,
            description=(
                "FDE Platform cross-turn/cross-session agent memory (not the knowledge graph)"
            ),
            event_expiry_duration=_MEMORY_EVENT_EXPIRY_DAYS,
            memory_execution_role_arn=memory_role.role_arn,
            memory_strategies=[
                bedrockagentcore.CfnMemory.MemoryStrategyProperty(
                    semantic_memory_strategy=bedrockagentcore.CfnMemory.SemanticMemoryStrategyProperty(
                        name="fde_semantic",
                        description=(
                            "Durable engagement-scoped working notes an agent has "
                            "surfaced this session -- NOT graph facts about the "
                            "customer's business (those go through kg_propose/HITL, "
                            "never through memory)."
                        ),
                        namespace_templates=[_NAMESPACE_TEMPLATE],
                    )
                ),
                bedrockagentcore.CfnMemory.MemoryStrategyProperty(
                    summary_memory_strategy=bedrockagentcore.CfnMemory.SummaryMemoryStrategyProperty(
                        name="fde_summary",
                        description=(
                            "Rolling session summaries so a long HITL-gate wait or "
                            "a human-kind workflow step can resume without "
                            "replaying the full transcript."
                        ),
                        namespace_templates=[_NAMESPACE_TEMPLATE],
                    )
                ),
                bedrockagentcore.CfnMemory.MemoryStrategyProperty(
                    user_preference_memory_strategy=bedrockagentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                        name="fde_user_pref",
                        description=(
                            "Durable per-reviewer preferences that follow a human "
                            "across engagements (e.g. hitl.reviewer.principal), "
                            "never a graph fact."
                        ),
                        namespace_templates=[_NAMESPACE_TEMPLATE],
                    )
                ),
            ],
        )
        self.memory_id = self.memory.attr_memory_id
        self.memory_arn = self.memory.attr_memory_arn

        # --- Runtimes ---
        # Pinned: `CfnRuntime`/`AgentRuntimeArtifactProperty`/
        # `ContainerConfigurationProperty`/`NetworkConfigurationProperty`/
        # `LifecycleConfigurationProperty` (local introspection + context7).
        # Cross-checked against `runtimes.py`'s `create_runtime` (:131-158),
        # `_build_artifact` (:59-82, container branch), `_environment_
        # variables` (:117-128).
        #
        # Container URI through the pull-through cache (brief's own shape):
        # `{account}.dkr.ecr.{region}.amazonaws.com/ecr-public/{alias}/
        # fde-{agent}:{tag}` -- NOT `public.ecr.aws/{alias}/...`
        # (`services.py`'s `ECR_PUBLIC_BASE`, which Fargate pulls directly
        # since ECS tasks CAN reference a public registry; AgentCore Runtime
        # cannot, hence this cache rule).
        cached_registry = cdk.Fn.join(
            "",
            [
                cdk.Aws.ACCOUNT_ID,
                ".dkr.ecr.",
                cdk.Aws.REGION,
                f".amazonaws.com/ecr-public/{ECR_PUBLIC_ALIAS}",
            ],
        )
        resolved_model_id = model_id_env(params)
        provider_api_key = dynamic_secret_env_value(provider_api_key_secret, params)

        # --- Supplemental read grant + secret reference for FDE_DB_SECRET_ARN ---
        # Same mismatch class `gate.py`'s own module docstring documents and
        # fixes for `gate_role`: `runtime_role`'s OWN template
        # (`runtime-permissions-policy.json`'s `ReadTheDbSecret` statement)
        # only grants `secretsmanager:GetSecretValue` on the Aurora
        # cluster's MASTER secret (`iam_roles.py`'s `FDE_DB_SECRET_ARN`
        # substitution = `db_secret.secret_arn`), not `fde/db/agent` (the
        # migration-minted login secret this construct points
        # `FDE_DB_SECRET_ARN` at below). Left alone, every runtime's direct
        # DB access -- `tracing.py`'s `raw_sql` writes, per `mcp_tools.py`'s
        # own docstring -- would get a `GetSecretValue` `AccessDenied`
        # rather than a working DSN. `add_to_principal_policy` lands this
        # in a separate, CDK-auto-generated `AWS::IAM::Policy` (not merged
        # into the existing `runtime-permissions` inline policy), same as
        # `gate.py`'s identical fix does for `gate_role`.
        login_secret_grant = runtime_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ReadRuntimeLoginSecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[fde_db_secrets_wildcard_arn()],
            )
        )
        runtime_db_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "RuntimeDbSecretRef", _RUNTIME_DB_SECRET_NAME
        )

        # --- C2 fix (final-fix-report.md): runtimes cannot pull images ---
        # `runtime-permissions-policy.json`'s own `ECRPullForContainerArtifact`
        # statement grants `ecr:BatchGetImage`/`ecr:GetDownloadUrlForLayer`
        # on `repository/fde-*-agent` -- a repo PATH this stack's runtimes
        # never pull from. Every runtime's real `container_uri` (below)
        # resolves through the ECR-Public pull-through cache at
        # `repository/ecr-public/{alias}/fde-{agent}`, an entirely
        # different repository path the template's own IAM grant does not
        # cover at all -- every runtime's first (and every subsequent)
        # image pull would fail with `AccessDenied`. A pull-through cache's
        # cached repository is also not a pre-existing resource: the FIRST
        # pull of any given tag additionally needs
        # `ecr:BatchImportUpstreamImage` (import the layer from
        # `public.ecr.aws` into the private cache) and
        # `ecr:CreateRepository` (the cached repo is created lazily, on
        # that first pull, not by `CfnPullThroughCacheRule` itself -- see
        # this module's own docstring). `ecr:GetAuthorizationToken` is
        # already granted, unconditionally, by the SAME template's
        # `ECRAuthToken` statement (`Resource: "*"`, since that action has
        # no resource-level permissions) -- verified by reading the
        # rendered policy before adding anything here, so it is not
        # duplicated. Supplemental grant, same pattern as
        # `ReadRuntimeLoginSecret` immediately above: a separate,
        # CDK-auto-generated `AWS::IAM::Policy`, not merged into the
        # existing `runtime-permissions` inline policy.
        # VPC-mode runtimes almost certainly manage ENIs in our subnets the
        # way every VPC-attached compute service does (Lambda's
        # AWSLambdaVPCAccessExecutionRole precedent). Not independently
        # live-verified whether AgentCore uses the execution role or a
        # service-linked role for this -- granted preemptively because the
        # actions are harmless if unused and a missing grant costs a full
        # launch cycle to discover. ENI actions don't support meaningful
        # resource-level scoping for the create/describe path.
        vpc_eni_grant = runtime_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="VpcEniManagementForVpcNetworkMode",
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:CreateNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DeleteNetworkInterface",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeVpcs",
                ],
                resources=["*"],
            )
        )
        ecr_pull_grant = runtime_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="PullThroughCacheEcrPublicImages",
                effect=iam.Effect.ALLOW,
                actions=[
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchImportUpstreamImage",
                    "ecr:CreateRepository",
                ],
                resources=[
                    cdk.Fn.join(
                        "",
                        [
                            "arn:aws:ecr:",
                            cdk.Aws.REGION,
                            ":",
                            cdk.Aws.ACCOUNT_ID,
                            f":repository/ecr-public/{ECR_PUBLIC_ALIAS}/*",
                        ],
                    )
                ],
            )
        )

        # --- v0.3.0-rc4 pivot: VPC networking for every runtime ---
        # One shared security group (egress-all -- the runtimes reach
        # Bedrock/CloudWatch/X-Ray over the internet through `vpc`'s NAT
        # gateway, and Aurora over the private route opened below; nothing
        # here needs a narrower egress rule the way `Database.security_
        # group`'s ingress-only posture does). `CfnRuntime` is an L1 with no
        # `connections`/auto-created-SG convenience the way `lambda_.
        # Function(vpc=...)` has, so this is built explicitly, the same way
        # `Database.security_group` is.
        self.runtime_security_group = ec2.SecurityGroup(
            self,
            "RuntimeSecurityGroup",
            vpc=vpc,
            description=(
                "AgentCore runtimes (Engagement/Workflow/Development) -- "
                "in-container stdio MCP, no Gateway transport (see agents.py "
                "module docstring)."
            ),
            allow_all_outbound=True,
        )
        # Same `Database.security_group`'s own docstring / `Migrations`'/
        # `GateService`'s idiom: open 5432 FROM the runtime security group,
        # using the cluster's own default-port helper rather than a
        # hand-typed `ec2.Port.tcp(5432)`.
        db_cluster.connections.allow_default_port_from(
            self.runtime_security_group,
            "AgentCore runtimes -> Aurora (stdio MCP + tracing)",
        )
        # `CfnRuntime.VpcConfigProperty` takes raw subnet-id strings, not an
        # `ec2.SubnetSelection` -- resolve it once here (same private-with-
        # egress tier `Migrations`/`GateService`/`Services` already use).
        runtime_subnet_ids = vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        ).subnet_ids

        self.runtimes = {}
        self.runtime_arns = {}
        for agent_name in AGENT_NAMES:
            container_uri = cdk.Fn.join(
                "",
                [cached_registry, f"/fde-{agent_name}:", params.release_tag.value_as_string],
            )
            runtime = bedrockagentcore.CfnRuntime(
                self,
                f"Runtime{agent_name.capitalize()}",
                # LIVE-VALIDATED CORRECTION (v0.3.0-rc2 launch, 2026-08-04):
                # AgentRuntimeName's control-plane pattern is
                # [a-zA-Z][a-zA-Z0-9_]{0,47} -- hyphens rejected outright
                # (same charset family as memory.py's strategy-name bug).
                # The repo's own deploy CLI (`runtimes.py`) uses the
                # hyphenated form and shares this latent bug -- ledgered as
                # a follow-up; underscores here.
                agent_runtime_name=f"fde_{agent_name}_agent",
                description=f"FDE Platform {agent_name} agent",
                agent_runtime_artifact=bedrockagentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                    container_configuration=bedrockagentcore.CfnRuntime.ContainerConfigurationProperty(
                        container_uri=cdk.Token.as_string(container_uri),
                    )
                ),
                role_arn=runtime_role.role_arn,
                # LIVE-VALIDATED (v0.3.0-rc4 pivot): `NetworkMode`'s live
                # registry schema enum is `["PUBLIC", "VPC"]` (`aws
                # cloudformation describe-type --type RESOURCE --type-name
                # AWS::BedrockAgentCore::Runtime`, definitions.NetworkMode)
                # -- "VPC" is the exact, case-sensitive control-plane value,
                # not cfn-lint's bundled copy (which had already been shown
                # stale once this rc, on the GatewayTarget endpoint
                # pattern -- see module docstring). `VpcConfigProperty`
                # requires both `security_groups`/`subnets` non-empty.
                network_configuration=bedrockagentcore.CfnRuntime.NetworkConfigurationProperty(
                    network_mode="VPC",
                    network_mode_config=bedrockagentcore.CfnRuntime.VpcConfigProperty(
                        security_groups=[self.runtime_security_group.security_group_id],
                        subnets=runtime_subnet_ids,
                    ),
                ),
                lifecycle_configuration=bedrockagentcore.CfnRuntime.LifecycleConfigurationProperty(
                    idle_runtime_session_timeout=_IDLE_TIMEOUT_S,
                    max_lifetime=_MAX_LIFETIME_S,
                ),
                environment_variables={
                    "FDE_AGENT_NAME": agent_name,
                    # `runtimes.py`'s own `_environment_variables()` sets
                    # this whenever `--region`/`AWS_REGION` is available,
                    # for exactly the reason it must be set here
                    # unconditionally: `mcp_tools.build_mcp_client`'s
                    # stdio-transport branch (the ONLY MCP-connection path a
                    # deployed runtime takes now -- see module docstring's
                    # "As-built network architecture (v1)") spawns
                    # `python -m fde_mcp`, which resolves the region through
                    # `fde_mcp.config.DatabaseSettings`, reading
                    # `AWS_REGION`/`AWS_DEFAULT_REGION` and raising
                    # `RuntimeError` if neither is set -- unlike ECS/Lambda,
                    # an AgentCore Runtime container does not get this
                    # injected for free, so it must be an explicit env var
                    # here.
                    "AWS_REGION": cdk.Aws.REGION,
                    # `fde/db/agent`'s ARN -- see `_RUNTIME_DB_SECRET_NAME`
                    # and the supplemental `ReadRuntimeLoginSecret` grant
                    # above. Needed both for `tracing.py`'s direct `raw_sql`
                    # writes AND for the in-container stdio `fde_mcp`
                    # subprocess's own DB connection (`mcp_tools.py`'s own
                    # docstring) -- without it, neither has a DSN source.
                    # This secret is now genuinely reachable: the runtime is
                    # VPC-attached (`self.runtime_security_group`, above)
                    # with a private route to the Aurora cluster the
                    # secret's DSN points at -- not the PUBLIC-network-mode
                    # dead end an earlier revision of this module recorded
                    # as an open risk.
                    "FDE_DB_SECRET_ARN": runtime_db_secret.secret_arn,
                    # Baked for audit parity (brief's own phrase, matching
                    # `runtimes.py`'s `_resolved_model_id` docstring: "every
                    # provisioned runtime carries a concrete FDE_MODEL_ID
                    # and the audit trail records exactly what runs") --
                    # the SAME `params.model_id_env` value `gate.py`'s
                    # inert copy carries, so the two can never disagree.
                    "FDE_MODEL_ID": resolved_model_id,
                    # These runtimes -- not gate.py's Lambda -- are the
                    # real consumers of the provider config (module
                    # docstring). `FDE_MODEL_COMPAT_PRESET` is NOT set:
                    # v1's `LaunchParams` has no CfnParameter for it (only
                    # `CompatBaseUrl`), the same simplification `gate.py`'s
                    # `provider_api_key_secret` docstring already documents
                    # for the API-key secret.
                    "FDE_MODEL_PROVIDER": params.model_provider.value_as_string,
                    "FDE_MODEL_BASE_URL": params.compat_base_url.value_as_string,
                    "FDE_MODEL_API_KEY": provider_api_key,
                    # `FDE_GATEWAY_URL`/`FDE_GATEWAY_SCOPES` are
                    # DELIBERATELY absent (v0.3.0-rc4 pivot): their absence
                    # is exactly what makes `GatewaySettings.url` falsy and
                    # selects `mcp_tools.build_mcp_client`'s in-container
                    # stdio transport over a Gateway connection -- see
                    # module docstring's "As-built network architecture
                    # (v1)". Do not re-add either without also wiring a
                    # real `CfnGatewayTarget` (currently absent, by owner
                    # decision) -- setting `FDE_GATEWAY_URL` alone would
                    # point every runtime at a Gateway with no MCP target
                    # behind it.
                },
            )
            # No CDK-inferred dependency exists between a runtime and the
            # cache rule (see module docstring) -- add it explicitly so
            # CloudFormation never attempts to create a runtime before the
            # rule its artifact resolves through exists.
            runtime.add_resource_dependency(self.pull_through_cache_rule)
            # LIVE-VALIDATED CORRECTION (v0.3.0-rc3 launch, 2026-08-04):
            # CreateAgentRuntime validates the ECR URI synchronously at
            # create time, and the supplemental grants above land in a
            # SEPARATE CDK-generated AWS::IAM::Policy resource -- the
            # runtime referencing only role_arn gave CloudFormation no
            # reason to wait for that policy, so validation raced the
            # attachment and failed with "Access denied while validating
            # ECR URI". Depend on both grants' policy resources explicitly.
            for grant in (ecr_pull_grant, login_secret_grant, vpc_eni_grant):
                if grant.policy_dependable is not None:
                    runtime.node.add_dependency(grant.policy_dependable)
            self.runtimes[agent_name] = runtime
            self.runtime_arns[agent_name.upper()] = runtime.attr_agent_runtime_arn
