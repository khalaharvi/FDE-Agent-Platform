from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from fde_cdk.database import Database
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
