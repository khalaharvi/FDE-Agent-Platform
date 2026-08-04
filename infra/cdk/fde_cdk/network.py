"""VPC network construct: the private network every later resource that
needs to reach the database attaches to (the Aurora cluster itself, and in
later tasks the gate Lambda / AgentCore runtimes).

Fixed topology, no `LaunchParams` dependency -- unlike the database, network
shape does not change between the `demo` and `production` deploy tiers.
"""

from __future__ import annotations

import aws_cdk.aws_ec2 as ec2
from constructs import Construct


class Network(Construct):
    """Two AZs, one NAT gateway, public + private-with-egress subnets only.

    No isolated subnet tier: nothing in this stack needs one, and adding a
    third subnet type would only inflate the VPC's IP footprint and the
    template's resource count for no consumer. One NAT gateway (not one per
    AZ) is a deliberate cost choice for a launch-button demo stack, not an
    oversight -- it is a single point of failure for private-subnet egress,
    which is an acceptable trade for this platform's blast radius.
    """

    vpc: ec2.Vpc

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )
