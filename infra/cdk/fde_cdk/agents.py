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
     `CfnRuntime`/`CfnGateway`/`CfnGatewayTarget`/`CfnMemory` and every
     `*Property` type this module constructs. This is authoritative --
     it is literally the code that will run.
  2. Context7 (`aws_cdk.aws_bedrockagentcore`, AWS CDK Python Reference)
     as a second, independent source. Agreed exactly with (1) on every
     field name used below (`agentRuntimeArtifact`/`networkConfiguration`/
     `authorizerConfiguration`/`customJwtAuthorizer`/`discoveryUrl`/
     `allowedAudience`, `protocolConfiguration.mcp.searchType`, etc.).
  3. Cross-checked a third time against this repo's own boto3 payloads --
     `packages/fde-agents/src/fde_agents/deploy/runtimes.py`'s
     `create_runtime` (:131), `_build_artifact` (:59), `_environment_
     variables` (:117), `_authorizer_configuration` (:85); `gateway.py`'s
     `create_gateway` (:83), `create_mcp_server_target` (:119); `memory.py`'s
     `create_memory` (:148) plus its three `_*_strategy` builders (:76-117)
     -- the CFN property names are the same words as the boto3 kwargs,
     just re-cased (`containerUri` -> `container_uri`, etc.), confirming
     the CFN resource types are a direct mirror of the control-plane API
     these scripts already call by hand.

Construction order inside this construct: pull-through cache rule -> Gateway
-> GatewayTarget -> Memory -> Runtimes. Gateway before Runtimes specifically
resolves a circular-dependency risk the brief calls out: the three runtimes'
`FDE_GATEWAY_URL` env needs the Gateway's URL, while the Gateway's own
target points at the MCP ALB (`services.mcp_url`), never at a runtime -- so
there is no cycle, only an ordering constraint, and building Gateway first
satisfies it both in Python construction order and in the CloudFormation
dependency graph (`self.gateway_url` is a `Fn::GetAtt` token threaded into
each runtime's `environment_variables`, so CDK adds the real `DependsOn`
automatically; the same is not true of the container URI passed to each
runtime -- see `EcrPublicPullThroughCache` note below).

`FDE_GATEWAY_URL` mechanism
----------------------------
`CfnGateway.attr_gateway_url` IS exposed as a CloudFormation attribute in
this lib version (confirmed via introspection: `'attr_gateway_url' in
dir(CfnGateway)`), so the brief's documented fallback (`Fn::Sub` over the
gateway id per `mcp_tools.py`'s documented URL shape,
`https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp`)
is NOT needed -- `self.gateway.attr_gateway_url` is used directly. Whatever
string AgentCore's control plane returns as `GatewayUrl` is definitionally
the same value `mcp_tools.py`'s `_mint_gateway_bearer_token`-adjacent client
code expects in `FDE_GATEWAY_URL` (`GatewaySettings.url`,
`fde_agents/common/config.py`), since both read the same control-plane
response shape.

Known launch-readiness gap -- ONE named Task-10 live-test risk, three
surfaces
---------------------------------------------------------------------------
Both the Gateway and all three Runtimes are, by this task's own design,
OUTSIDE the VPC: `CfnGateway` has no VPC/network property at all in this
lib version (confirmed: no such field in its constructor signature) -- an
AgentCore Gateway is always a fully-managed, non-VPC-attached service -- and
every `CfnRuntime` here is built with `network_mode="PUBLIC"` (the brief's
own required shape). That single fact creates a reachability question on
THREE distinct network paths into this stack's VPC-internal resources, not
one:

  1. **Gateway -> internal ALB.** The Gateway's `mcpServer` target
     (`self.gateway_target`) points at `services.mcp_url`, which is
     `services.py`'s INTERNAL ALB (no public IP, no IGW route). Whether a
     non-VPC Gateway can reach it at all is unresolved here, the same way
     `gateway.py`'s own boto3 script does not resolve it either (it just
     takes `--mcp-server-endpoint` as an opaque URL with no network wiring
     of its own).
  2. **Runtime -> Aurora, for tracing.** Every deployed runtime's
     `FDE_DB_SECRET_ARN` env (added below) points `tracing.py`'s direct
     `fde_mcp.db`/`raw_sql` writes (`mcp_tools.py`'s own docstring: "the
     one sanctioned exception... writes those two tables directly") at the
     Aurora cluster -- but that cluster lives in `network.py`'s VPC private
     subnets with no public endpoint, and a PUBLIC-network-mode runtime has
     no private route to it.
  3. **Runtime -> Aurora, for the local-stdio MCP fallback.**
     `GatewaySettings.url` (`fde_agents/common/config.py`) selects
     Gateway-vs-stdio transport by presence; every runtime here always gets
     `FDE_GATEWAY_URL` set (below), so this path should never trigger in
     this deployment -- but the code path exists (`mcp_tools.build_mcp_
     client`'s dev-mode branch spawns `python -m fde_mcp` as a local
     subprocess, and THAT process needs its own DB connection), and it
     would share the identical PUBLIC-network-mode reachability gap the
     moment it ever did trigger (e.g. a future change that leaves
     `FDE_GATEWAY_URL` unset).

All three share one root cause (PUBLIC network mode = no VPC attachment =
no private route to anything in `network.py`'s VPC) and are recorded here
as a single follow-up rather than three separate ones. This is the same
class of "written against verified API shapes, not validated against live
AWS" gap the repo's honesty rule already names for other AWS-touching code.

**The documented fix path, if a live test confirms the failure:**
`CfnRuntime.NetworkConfigurationProperty` has an optional
`network_mode_config` field accepting a `VpcConfigProperty(security_groups:
Sequence[str], subnets: Sequence[str])` (confirmed via introspection: both
types exist on this installed `aws-cdk-lib==2.263.0`) -- i.e. this lib
version's schema DOES structurally support attaching a runtime to a VPC,
mirroring the `subnets`/`security_groups` shape `ec2.SubnetSelection`
already produces for every other VPC-attached compute in this stack
(`Migrations`/`GateService`/`Services`). This would fix surfaces 2 and 3
directly (a VPC-attached runtime reaching Aurora the same way the gate
Lambda already does) and, if AgentCore Gateway ever gains an equivalent
VPC-attachment property in a later lib version, surface 1 the same way.
NOT changed here (out of scope for this task, and `network_mode`'s exact
non-"PUBLIC" enum string was not itself verified -- only that the
`network_mode_config` property exists and accepts a VPC shape).

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
CDK's automatic same-token dependency inference does not apply here (unlike
the Gateway-URL case above). Each runtime instead gets an explicit
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
import aws_cdk.aws_ecr as ecr
import aws_cdk.aws_iam as iam
import aws_cdk.aws_secretsmanager as secretsmanager
from constructs import Construct

from fde_cdk.identity import GATEWAY_INVOKE_SCOPE_NAME, GATEWAY_RESOURCE_SERVER_ID
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

# Verbatim from `fde_agents.deploy.gateway.create_mcp_server_target`
# (:119-144).
_GATEWAY_TARGET_NAME = "fde-kg-mcp-server"
_GATEWAY_TARGET_DESCRIPTION = (
    "FDE knowledge-graph MCP server (fde_mcp.server, FDE_MCP_TRANSPORT=http)"
)

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

# I4(b) fix (final-fix-report.md): built from `identity.py`'s own two
# constants (never a second hand-copied literal) so this can never drift
# from the resource-server/scope pair `m2m_client`'s own OAuth scope is
# minted against. Cognito's own convention joins `ResourceServerIdentifier`
# and `ScopeName` with "/" -- "gateway/invoke" -- which is NOT
# `fde_agents.common.config.GatewaySettings.scopes`' own unset-env-var
# default ("gateway:invoke", colon-separated: see that module's docstring,
# `config.py:227-229`), so every deployed runtime needs FDE_GATEWAY_SCOPES
# set explicitly to the real, slash-separated value or every M2M token
# request would ask Cognito for a scope that does not exist.
FDE_GATEWAY_SCOPE = f"{GATEWAY_RESOURCE_SERVER_ID}/{GATEWAY_INVOKE_SCOPE_NAME}"

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
    every runtime's container artifact resolves through. `gateway` +
    `gateway_target`: the AgentCore Gateway fronting `services.mcp_url`,
    CUSTOM_JWT-authorized against Cognito. `memory`: the always-on semantic/
    summary/user-preference AgentCore Memory resource. `runtimes`: the three
    AgentCore Runtimes, keyed by lowercase agent name. `runtime_arns`:
    the same three runtimes' ARNs, keyed by the UPPERCASE name `gate.py`'s
    `FDE_RUNTIME_ARN_{AGENT}` envs expect."""

    pull_through_cache_rule: ecr.CfnPullThroughCacheRule
    gateway: bedrockagentcore.CfnGateway
    gateway_target: bedrockagentcore.CfnGatewayTarget
    gateway_url: str
    memory: bedrockagentcore.CfnMemory
    memory_id: str
    memory_arn: str
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
        runtime_role: iam.Role,
        gateway_service_role: iam.Role,
        memory_role: iam.Role,
        jwt_discovery_url: str,
        jwt_allowed_audience: str,
        m2m_client_secret: cdk.SecretValue,
        mcp_url: str,
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
            credential_provider_vendor=bedrockagentcore.OAuth2CredentialProviderVendor.COGNITO.value,
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

        # --- Gateway target: the MCP ALB, not the runtimes (see module
        # docstring's circular-dependency note) ---
        # Pinned: `CfnGatewayTarget`/`TargetConfigurationProperty`/
        # `McpTargetConfigurationProperty`/`McpServerTargetConfiguration
        # Property`/`CredentialProviderConfigurationProperty` (local
        # introspection + context7). Cross-checked against `gateway.py`'s
        # `create_mcp_server_target` (:119-144): `targetConfiguration.mcp.
        # mcpServer.endpoint`/`.listingMode="DEFAULT"`,
        # `credentialProviderConfigurations=[{"credentialProviderType":
        # "GATEWAY_IAM_ROLE"}]`. `mcp_url` already carries the `/mcp` suffix
        # (`services.py`'s own `self.mcp_url = f"http://{alb.dns}/mcp"`).
        self.gateway_target = bedrockagentcore.CfnGatewayTarget(
            self,
            "GatewayMcpServerTarget",
            gateway_identifier=self.gateway.attr_gateway_identifier,
            name=_GATEWAY_TARGET_NAME,
            description=_GATEWAY_TARGET_DESCRIPTION,
            target_configuration=bedrockagentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=bedrockagentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    mcp_server=bedrockagentcore.CfnGatewayTarget.McpServerTargetConfigurationProperty(
                        endpoint=mcp_url,
                        listing_mode="DEFAULT",
                    )
                )
            ),
            credential_provider_configurations=[
                bedrockagentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                )
            ],
        )

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
        runtime_role.add_to_principal_policy(
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
        runtime_role.add_to_principal_policy(
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
                agent_runtime_name=f"fde-{agent_name}-agent",
                description=f"FDE Platform {agent_name} agent",
                agent_runtime_artifact=bedrockagentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                    container_configuration=bedrockagentcore.CfnRuntime.ContainerConfigurationProperty(
                        container_uri=cdk.Token.as_string(container_uri),
                    )
                ),
                role_arn=runtime_role.role_arn,
                network_configuration=bedrockagentcore.CfnRuntime.NetworkConfigurationProperty(
                    network_mode="PUBLIC",
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
                    # unconditionally: `mcp_tools._mint_gateway_bearer_
                    # token` (the only MCP-connection path a deployed
                    # runtime takes, since `FDE_GATEWAY_URL` is always set
                    # below) resolves the region through `fde_mcp.config.
                    # DatabaseSettings`, which reads `AWS_REGION`/
                    # `AWS_DEFAULT_REGION` and raises `RuntimeError` if
                    # neither is set -- unlike ECS/Lambda, an AgentCore
                    # Runtime container does not get this injected for
                    # free, so it must be an explicit env var here.
                    "AWS_REGION": cdk.Aws.REGION,
                    # `fde/db/agent`'s ARN -- see `_RUNTIME_DB_SECRET_NAME`
                    # and the supplemental `ReadRuntimeLoginSecret` grant
                    # above. Needed for `tracing.py`'s direct `raw_sql`
                    # writes (`mcp_tools.py`'s own docstring); without it
                    # every trace write has no DSN source and silently
                    # no-ops rather than recording the turn.
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
                    # Presence (not truthiness) selects Gateway transport
                    # over local stdio (`GatewaySettings.url`,
                    # `fde_agents/common/config.py`) -- deployed runtimes
                    # always get a real URL here, never blank.
                    "FDE_GATEWAY_URL": self.gateway_url,
                    # I4(b) fix (final-fix-report.md): without this,
                    # `GatewaySettings.scopes` falls back to its own
                    # unset-env-var default ("gateway:invoke", COLON --
                    # `fde_agents/common/config.py:227-229`), which is not
                    # a scope `m2m_client` was ever minted against
                    # (Cognito's own separator is "/" -- `identity.py`).
                    # Every M2M token request would then ask for a scope
                    # that doesn't exist and fail. See module-level
                    # `FDE_GATEWAY_SCOPE`.
                    "FDE_GATEWAY_SCOPES": FDE_GATEWAY_SCOPE,
                },
            )
            # No CDK-inferred dependency exists between a runtime and the
            # cache rule (see module docstring) -- add it explicitly so
            # CloudFormation never attempts to create a runtime before the
            # rule its artifact resolves through exists.
            runtime.add_resource_dependency(self.pull_through_cache_rule)
            self.runtimes[agent_name] = runtime
            self.runtime_arns[agent_name.upper()] = runtime.attr_agent_runtime_arn
