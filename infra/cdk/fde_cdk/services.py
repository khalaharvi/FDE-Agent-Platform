"""`Services`: one ECS cluster, the `mcp_service` Fargate task behind an
**internal** ALB, and the `embedder_service` Fargate task (no load balancer
-- it is a queue-polling worker, not something anything calls).

Both task definitions run the SAME container image
(`{ECR_PUBLIC_BASE}/fde-mcp:{ReleaseTag}`): `fde-embedder` is a console
script inside the `fde-mcp` Python package (`packages/fde-mcp/pyproject.
toml`'s `[project.scripts]`, `fde-embedder = "fde_mcp.embedder_worker:
main"`), so the one image already contains both entrypoints -- the embedder
task definition just overrides `command=["fde-embedder"]` instead of the
image's default CMD (`fde-mcp`). This is the "same task family" the plan's
brief describes, read as "same image, different command" rather than a
literal shared `AWS::ECS::TaskDefinition` (ECS task definitions do not
support a per-Service command override; two containers that need different
commands need two task definitions).

The internal-ALB health-check decision
---------------------------------------
`packages/fde-mcp/src/fde_mcp/server.py` (read directly for this task) has
no dedicated health-check route: `build_server()`'s `FastMCP(...)` call
sets no `custom_route`, and `main()` only ever calls `mcp.run(transport=
"streamable-http")` -- so the ONLY HTTP path the container ever answers on
is `/mcp` itself (`mcp.settings.streamable_http_path`, the FastMCP default).
The MCP Python SDK's `streamable_http_app()` (`mcp/server/fastmcp/server.
py` in this venv, read directly) mounts that ASGI session-manager app on
`/mcp` for every HTTP method (Starlette does not restrict methods on a
route whose endpoint is a raw ASGI callable, only on function-based
endpoints) -- so a plain unauthenticated `GET /mcp` with no `Mcp-Session-
Id` header (exactly what an ALB health check sends) reaches the streamable-
HTTP session manager and is rejected by IT, not by routing: the streamable-
HTTP spec requires a `POST` with a JSON-RPC body to establish a session, or
a valid existing session id for a `GET` (server-to-client SSE stream) --
neither is true for a bare health-check `GET`, so the expected response is
a 4xx (400/406, not 200, and not a 5xx -- the process is up and answering
HTTP, it is just rejecting a malformed MCP request). This was NOT verified
against a running container (AWS honesty rule: no code here has run
against live AWS or even a live container) -- it is read directly from both
`fde_mcp/server.py` and the installed `mcp` SDK's routing code, not
guessed.

Given that, `elbv2.HealthCheck(path="/mcp", healthy_http_codes="200-499")`
is the health check this construct uses: broad enough to treat "the process
answered with SOME HTTP response" as healthy (the actual failure mode this
health check exists to catch -- container crashed, still starting, out of
memory -- produces a connection failure or a 5xx, not a 4xx), narrow enough
to still fail the checks that matter (connection refused, 500s, timeouts).
The alternative the brief also names -- a bare TCP health check on port
8080 -- would be strictly weaker (a process wedged inside the ASGI stack
but still holding the listening socket open would pass a TCP check and fail
this one). The right long-term fix is a dedicated `@mcp.custom_route(
"/healthz", methods=["GET"])` in `fde_mcp/server.py` itself (the SDK
explicitly supports this, see that module's own docstring/example) --
that is an `fde-mcp` package change, out of scope for this CDK-only task,
and is called out in the Task 6 report as a follow-up.
"""

from __future__ import annotations

import aws_cdk as cdk
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_ecs as ecs
import aws_cdk.aws_elasticloadbalancingv2 as elbv2
import aws_cdk.aws_iam as iam
import aws_cdk.aws_logs as logs
import aws_cdk.aws_rds as rds
import aws_cdk.aws_secretsmanager as secretsmanager
from constructs import Construct

from fde_cdk.params import (
    ECR_PUBLIC_BASE,
    LaunchParams,
    dynamic_secret_env_value,
    fde_db_secrets_wildcard_arn,
)

_MCP_CONTAINER_PORT = 8080

# infra/cdk/lambdas/migration_runner/handler.py's LOGIN_SECRETS -- the
# per-role login secrets the migration custom resource mints. GateService
# reads "fde/db/gate"; these two are this construct's pair.
_MCP_DB_SECRET_NAME = "fde/db/agent"
_EMBEDDER_DB_SECRET_NAME = "fde/db/ingest"

# `fde_mcp.embedder_worker`'s FDE_EMBEDDER_ROLE default IS already
# "fde_ingest" (packages/fde-mcp/src/fde_mcp/config.py) -- set explicitly
# here anyway per the brief, so the deployed environment states its own
# role rather than relying on a default a future config.py change could
# silently move.
_EMBEDDER_ROLE = "fde_ingest"


def _fargate_task_role(scope: Construct, construct_id: str, *, read_secret_sid: str) -> iam.Role:
    """A minimal ECS task role: assumed by `ecs-tasks.amazonaws.com`, one
    inline statement granting `secretsmanager:GetSecretValue` on the three
    migration-minted login secrets (`fde_db_secrets_wildcard_arn()`,
    `params.py` -- same pattern `GateService`'s supplemental grant on
    `gate_role` uses, see that module's docstring for why the wildcard and
    not a narrower single-secret ARN). No repo template exists for this
    role (unlike `iam_roles.py`'s `runtime_role`/`gate_role`) because
    Fargate task roles for the MCP/embedder services are new in this task,
    not a pre-existing deploy script's documented IAM need.
    """
    role = iam.Role(
        scope,
        construct_id,
        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        inline_policies={
            "read-db-login-secret": iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid=read_secret_sid,
                        effect=iam.Effect.ALLOW,
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[fde_db_secrets_wildcard_arn()],
                    )
                ]
            )
        },
    )
    return role


class Services(Construct):
    """`cluster`: the one ECS cluster. `mcp_service` + `alb`: the MCP
    Fargate service behind an internal ALB (`mcp_url` = `http://{alb.dns}/
    mcp`, what `GateService` sets `FDE_MCP_URL` to). `embedder_service`: the
    `fde-embedder` worker, same image, no load balancer."""

    cluster: ecs.Cluster
    alb: elbv2.ApplicationLoadBalancer
    mcp_service: ecs.FargateService
    embedder_service: ecs.FargateService
    mcp_url: str
    # Task 7.5: `OpsLayer`'s `FdeMcpUnhealthyHosts`/`FdeMcpTarget5xx`
    # alarms build on this target group's own `metric_*` helpers -- exposed
    # here (captured from `add_targets`' return value, previously
    # discarded) so `stack.py` can pass it into `OpsLayer` as a handle
    # rather than that construct reaching into `Services`' internals.
    mcp_target_group: elbv2.ApplicationTargetGroup

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        params: LaunchParams,
        vpc: ec2.IVpc,
        db_cluster: rds.DatabaseCluster,
        provider_api_key_secret: secretsmanager.ISecret,
    ) -> None:
        super().__init__(scope, construct_id)

        self.cluster = ecs.Cluster(self, "Cluster", vpc=vpc)

        image = ecs.ContainerImage.from_registry(
            f"{ECR_PUBLIC_BASE}/fde-mcp:{params.release_tag.value_as_string}"
        )
        runtime_platform = ecs.RuntimePlatform(
            cpu_architecture=ecs.CpuArchitecture.ARM64,
            operating_system_family=ecs.OperatingSystemFamily.LINUX,
        )
        # Shared between the MCP and embedder containers: the embed
        # provider is one seam (params.py's embed_provider/embed_model_id),
        # independent of which model provider the gate/agents use.
        # FDE_EMBED_BASE_URL reuses CompatBaseUrl (params.py has no separate
        # embed-base-url parameter -- same v1 simplification as the shared
        # provider-api-key secret, see gate.py's provider_api_key_secret
        # docstring).
        embed_env = {
            "FDE_EMBED_PROVIDER": params.embed_provider.value_as_string,
            "FDE_EMBED_MODEL_ID": params.embed_model_id.value_as_string,
            "FDE_EMBED_BASE_URL": params.compat_base_url.value_as_string,
            "FDE_EMBED_API_KEY": dynamic_secret_env_value(provider_api_key_secret, params),
        }

        # --- MCP service ---
        mcp_db_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "AgentDbSecretRef", _MCP_DB_SECRET_NAME
        )
        mcp_task_role = _fargate_task_role(
            self, "McpTaskRole", read_secret_sid="ReadAgentLoginSecret"
        )
        mcp_task_definition = ecs.FargateTaskDefinition(
            self,
            "McpTaskDefinition",
            cpu=512,
            memory_limit_mib=1024,
            runtime_platform=runtime_platform,
            task_role=mcp_task_role,
        )
        # Task 7.5 hygiene: explicit LogGroup for the same reason gate.py's
        # gets one -- an `ecs.LogDriver.aws_logs` with no `log_group=`
        # override lets the ECS agent auto-create the group with NO
        # retention policy (logs kept forever) rather than this
        # `RetentionDays.ONE_MONTH`/`RemovalPolicy.DESTROY` pair. Auto-named
        # (no `log_group_name=`): unlike the gate Lambda, neither Fargate
        # task definition pins a fixed, externally-depended-on name, so
        # there is no pre-7.5-orphan collision risk to guard against here.
        mcp_log_group = logs.LogGroup(
            self,
            "McpLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        mcp_task_definition.add_container(
            "McpContainer",
            container_name="fde-mcp",
            image=image,
            port_mappings=[ecs.PortMapping(container_port=_MCP_CONTAINER_PORT)],
            environment={
                "FDE_MCP_TRANSPORT": "http",
                "FDE_DB_SECRET_ARN": mcp_db_secret.secret_arn,
                **embed_env,
            },
            logging=ecs.LogDriver.aws_logs(stream_prefix="fde-mcp", log_group=mcp_log_group),
        )
        self.mcp_service = ecs.FargateService(
            self,
            "McpService",
            cluster=self.cluster,
            task_definition=mcp_task_definition,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            desired_count=1,
        )
        db_cluster.connections.allow_default_port_from(
            self.mcp_service, "fde-mcp Fargate service connects to Postgres"
        )

        # internet_facing=False: Scheme=internal (test_alb_is_internal).
        # No launch parameter switches this -- v1 never exposes the MCP
        # server outside the VPC; only the gate Lambda (also VPC-attached)
        # and, later, the AgentCore Gateway target this ALB.
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "InternalAlb", vpc=vpc, internet_facing=False
        )
        # open=False + an explicit allow_from(vpc CIDR): the ALB's Scheme
        # already makes it unreachable from outside the VPC (no public IP,
        # no IGW route for a private-subnet ENI), but `open=True`'s literal
        # security-group rule is `0.0.0.0/0` regardless of that scheme --
        # verified via a standalone synth. Scoping the SG rule itself to
        # the VPC's own CIDR is a one-line hardening that costs nothing:
        # every real caller (the gate Lambda, later the AgentCore Gateway)
        # is inside this VPC anyway.
        listener = self.alb.add_listener(
            "Listener", port=80, protocol=elbv2.ApplicationProtocol.HTTP, open=False
        )
        listener.connections.allow_from(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(80),
            "VPC-internal callers only (gate Lambda, AgentCore Gateway)",
        )
        # Task 7.5: `add_targets` returns the `ApplicationTargetGroup` it
        # creates -- previously discarded, now captured as
        # `self.mcp_target_group` (see this construct's own docstring/class
        # attribute) for `OpsLayer`'s unhealthy-host/5xx alarms.
        self.mcp_target_group = listener.add_targets(
            "McpTargets",
            port=_MCP_CONTAINER_PORT,
            targets=[self.mcp_service],
            health_check=elbv2.HealthCheck(
                path="/mcp",
                healthy_http_codes="200-499",  # see module docstring
            ),
        )
        self.mcp_url = f"http://{self.alb.load_balancer_dns_name}/mcp"

        # --- Embedder service: same image, command override, no ALB ---
        embedder_db_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "IngestDbSecretRef", _EMBEDDER_DB_SECRET_NAME
        )
        embedder_task_role = _fargate_task_role(
            self, "EmbedderTaskRole", read_secret_sid="ReadIngestLoginSecret"
        )
        embedder_task_definition = ecs.FargateTaskDefinition(
            self,
            "EmbedderTaskDefinition",
            cpu=256,
            memory_limit_mib=512,
            runtime_platform=runtime_platform,
            task_role=embedder_task_role,
        )
        # Same Task 7.5 hygiene as McpLogGroup above.
        embedder_log_group = logs.LogGroup(
            self,
            "EmbedderLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        embedder_task_definition.add_container(
            "EmbedderContainer",
            container_name="fde-embedder",
            image=image,
            command=["fde-embedder"],
            environment={
                "FDE_DB_SECRET_ARN": embedder_db_secret.secret_arn,
                "FDE_EMBEDDER_ROLE": _EMBEDDER_ROLE,
                **embed_env,
            },
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="fde-embedder", log_group=embedder_log_group
            ),
        )
        self.embedder_service = ecs.FargateService(
            self,
            "EmbedderService",
            cluster=self.cluster,
            task_definition=embedder_task_definition,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            desired_count=1,
        )
        db_cluster.connections.allow_default_port_from(
            self.embedder_service, "fde-embedder Fargate service connects to Postgres"
        )
