from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from fde_cdk.agents import Agents
from fde_cdk.database import Database
from fde_cdk.gate import GateService, provider_api_key_secret
from fde_cdk.iam_roles import IamRoles
from fde_cdk.identity import Identity
from fde_cdk.migrations import Migrations
from fde_cdk.network import Network
from fde_cdk.ops import OpsLayer
from fde_cdk.outputs import add_outputs
from fde_cdk.params import add_launch_params
from fde_cdk.services import Services


class FdePlatformStack(cdk.Stack):
    """Root stack behind the README Launch-Stack button. One template URL,
    ten single-responsibility constructs: Network -> Database -> Identity
    -> IamRoles -> Migrations -> Services -> Agents -> GateService ->
    OpsLayer -> Outputs."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(
            scope,
            construct_id,
            description=(
                "FDE Agent Platform - core loop launch stack (knowledge graph, human gates, agents)"
            ),
            synthesizer=cdk.BootstraplessSynthesizer(),
            **kwargs,  # type: ignore[arg-type]
        )
        self.params = add_launch_params(self)

        # Network first: the database (and every later construct that
        # needs VPC egress -- gate Lambda, AgentCore runtimes) attaches to
        # `self.network.vpc`.
        self.network = Network(self, "Network")
        self.database = Database(self, "Database", params=self.params, vpc=self.network.vpc)

        # Identity (Cognito) has no dependency on Database -- it only reads
        # `params.admin_email` -- but comes after it anyway to match the
        # brief's fixed construct order (Network -> Database -> Identity ->
        # IamRoles), so the order every later task documents the stack by
        # is the same order the constructor actually builds it in.
        self.identity = Identity(self, "Identity", params=self.params)
        self.iam_roles = IamRoles(
            self,
            "IamRoles",
            db_secret=self.database.secret,
            db_cluster=self.database.cluster,
        )

        # Migrations: needs the network (VPC-attached Lambda), the database
        # (secret to connect with, cluster to open ingress from), and
        # IamRoles' `migration_role`.
        self.migrations = Migrations(
            self,
            "Migrations",
            params=self.params,
            vpc=self.network.vpc,
            db_secret=self.database.secret,
            db_cluster=self.database.cluster,
            migration_role=self.iam_roles.migration_role,
        )

        # Task 6: the one Secrets Manager mirror of ProviderApiKey, shared
        # by Services' FDE_EMBED_API_KEY and GateService's
        # FDE_MODEL_API_KEY (see gate.py's provider_api_key_secret
        # docstring for why it is built once here rather than inside
        # either construct).
        self.provider_api_key_secret = provider_api_key_secret(self, self.params)

        # Services before Agents/GateService: both need services.mcp_url
        # (the internal ALB's URL) -- Agents' Gateway target points at it
        # directly, GateService's FDE_MCP_URL env carries it too -- and
        # neither exists until Services is built.
        self.services = Services(
            self,
            "Services",
            params=self.params,
            vpc=self.network.vpc,
            db_cluster=self.database.cluster,
            provider_api_key_secret=self.provider_api_key_secret,
        )
        # Both Services and GateService read migration-minted login
        # secrets (fde/db/agent, fde/db/ingest, fde/db/gate) -- those
        # secrets do not exist until the Migrations custom resource has
        # run once, so both constructs must deploy strictly after it.
        self.services.node.add_dependency(self.migrations.resource)

        # Agents before GateService: GateService's FDE_RUNTIME_ARN_* envs
        # need the three real runtime ARNs Agents creates (see gate.py's
        # module docstring -- before this task, GateService took
        # `runtime_arns=None` and emitted "" placeholders; now it always
        # gets `agents.runtime_arns`). Agents itself is built with the
        # Gateway before the Runtimes internally (see agents.py's module
        # docstring for the ordering this resolves) -- that internal order
        # is independent of where `Agents(...)` sits in THIS constructor,
        # which only needs to be after `Services` (Gateway target =
        # `services.mcp_url`) and before `GateService` (runtime ARNs).
        self.agents = Agents(
            self,
            "Agents",
            params=self.params,
            runtime_role=self.iam_roles.runtime_role,
            gateway_service_role=self.iam_roles.gateway_service_role,
            memory_role=self.iam_roles.memory_role,
            jwt_discovery_url=self.identity.discovery_url,
            jwt_allowed_audience=self.identity.m2m_client.user_pool_client_id,
            mcp_url=self.services.mcp_url,
            provider_api_key_secret=self.provider_api_key_secret,
        )

        self.gate_service = GateService(
            self,
            "GateService",
            params=self.params,
            vpc=self.network.vpc,
            db_cluster=self.database.cluster,
            gate_role=self.iam_roles.gate_role,
            identity=self.identity,
            mcp_url=self.services.mcp_url,
            provider_api_key_secret=self.provider_api_key_secret,
            runtime_arns=self.agents.runtime_arns,
        )
        self.gate_service.node.add_dependency(self.migrations.resource)

        # `Identity.console_client`'s own docstring names this exact
        # follow-up: CDK fills CallbackURLs/LogoutURLs with its placeholder
        # default (https://example.com) when authorization-code-grant flows
        # are enabled and neither is set at construction time, because the
        # real console URL -- the gate API's own endpoint, `/ui` being the
        # console's root route (fde_gate/ui.py's router, api.py's own
        # printed "console: {endpoint}/ui") -- does not exist until
        # `GateService.http_api` does. Both now exist, so this stack
        # overrides the L1's `CallbackURLs`/`LogoutURLs` properties
        # directly (there is no L2 setter for either post-construction;
        # `UserPoolClient` only accepts them at construction time) rather
        # than leaving CDK's placeholder in the deployed template.
        console_url = f"{self.gate_service.http_api.api_endpoint}/ui"
        cfn_console_client = self.identity.console_client.node.default_child
        assert cfn_console_client is not None
        cfn_console_client.add_property_override("CallbackURLs", [console_url])
        cfn_console_client.add_property_override("LogoutURLs", [console_url])

        # OpsLayer after GateService: its 8 alarms/dashboard read handles
        # (`gate_service.function`, `migrations.function`,
        # `services.mcp_target_group`/`mcp_service`/`embedder_service`,
        # `database.cluster`, `gate_service.dlq`) every earlier construct
        # already built -- see ops.py's own module docstring for the
        # condition-gating mechanism (every OpsLayer resource carries
        # `Condition: OpsEnabled`) and the alarm inventory.
        self.ops = OpsLayer(
            self,
            "OpsLayer",
            params=self.params,
            gate_function=self.gate_service.function,
            migration_function=self.migrations.function,
            mcp_target_group=self.services.mcp_target_group,
            mcp_service=self.services.mcp_service,
            embedder_service=self.services.embedder_service,
            db_cluster=self.database.cluster,
            dlq=self.gate_service.dlq,
        )

        # Outputs last: every value they print (the console URL, the login
        # URL, the two endpoints, the ops topic/dashboard) is built from
        # constructs above them. `add_outputs` is a plain function, not a
        # nested Construct -- see outputs.py's module docstring for why
        # (clean, unhashed output names on the CloudFormation console).
        add_outputs(
            self,
            identity=self.identity,
            gate_service=self.gate_service,
            services=self.services,
            ops=self.ops,
            console_url=console_url,
        )
