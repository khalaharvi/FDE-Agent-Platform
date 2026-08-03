from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from fde_cdk.database import Database
from fde_cdk.iam_roles import IamRoles
from fde_cdk.identity import Identity
from fde_cdk.network import Network
from fde_cdk.params import add_launch_params


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
