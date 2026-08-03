from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from fde_cdk.database import Database
from fde_cdk.gate import GateService, provider_api_key_secret
from fde_cdk.iam_roles import IamRoles
from fde_cdk.identity import Identity
from fde_cdk.migrations import Migrations
from fde_cdk.network import Network
from fde_cdk.params import add_launch_params
from fde_cdk.services import Services


class FdePlatformStack(cdk.Stack):
    """Root stack behind the README Launch-Stack button. One template URL,
    eight single-responsibility constructs (added by later tasks)."""

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

        # Services before GateService: GateService's FDE_MCP_URL env needs
        # services.mcp_url (the internal ALB's URL), which does not exist
        # until Services is built. The eventual order (per the plan) is
        # Migrations -> Services -> Agents (Task 7) -> GateService, because
        # GateService's FDE_RUNTIME_ARN_* env vars need Task 7's runtime
        # ARNs; Agents does not exist yet, so GateService here takes
        # `runtime_arns=None` (placeholder "" envs -- see gate.py's module
        # docstring) and Task 7 re-wires this call with `agents.
        # runtime_arns` once it exists.
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
