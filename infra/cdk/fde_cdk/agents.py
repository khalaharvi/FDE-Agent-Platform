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

A known, undocumented-here gap: `CfnGateway` in this lib version has no VPC/
network property at all (confirmed: no such field in its constructor
signature) -- an AgentCore Gateway is a fully-managed, non-VPC-attached
service, so its `mcpServer` target reaching `services.py`'s INTERNAL ALB
(no public IP, no IGW route) is a real reachability question this CDK layer
does not resolve, the same way `gateway.py`'s own boto3 script does not
either (it just takes `--mcp-server-endpoint` as an opaque URL). This is the
same class of "written against verified API shapes, not validated against
live AWS" gap the repo's honesty rule already names for other AWS-touching
code; flagged here as a launch-readiness follow-up, not fixed in this task.

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

from fde_cdk.params import ECR_PUBLIC_ALIAS, LaunchParams, dynamic_secret_env_value, model_id_env

# Matches `fde_agents.deploy.runtimes.AGENT_NAMES` exactly (lowercase --
# the AgentCore API's own `agentRuntimeName`/`FDE_AGENT_NAME` case).
# `gate.py`'s `_AGENT_NAMES` is the uppercase form this module's
# `runtime_arns` dict keys use, matching what `GateSettings.runtime_arn_for`
# looks up.
AGENT_NAMES = ("engagement", "workflow", "development")

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
                ),
            ),
        )
        self.gateway_url = self.gateway.attr_gateway_url

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
                },
            )
            # No CDK-inferred dependency exists between a runtime and the
            # cache rule (see module docstring) -- add it explicitly so
            # CloudFormation never attempts to create a runtime before the
            # rule its artifact resolves through exists.
            runtime.add_resource_dependency(self.pull_through_cache_rule)
            self.runtimes[agent_name] = runtime
            self.runtime_arns[agent_name.upper()] = runtime.attr_agent_runtime_arn
