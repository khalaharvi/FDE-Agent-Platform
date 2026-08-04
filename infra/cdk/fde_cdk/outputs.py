"""`add_outputs`: the five `CfnOutput`s CloudFormation prints on the console
the moment this stack finishes deploying -- the whole point of the README's
one-click Launch-Stack button is that these five lines are enough to start
using the platform without reading a line of CDK or opening another AWS
console tab.

A plain function taking the STACK itself as `scope`, not a `Construct`
subclass with its own node -- same shape as `gate.py`'s
`provider_api_key_secret(scope, params)`, and for the identical reason:
CDK's logical-id generation (`makeUniqueId`) appends an 8-character hash of
the full construct path once a resource sits behind more than one path
segment, but leaves a DIRECT child of the Stack unhashed. A `CfnOutput`
built inside a nested `Outputs(Construct)` would show up in the template as
`OutputsReviewConsoleUrl76ECE7C8` (verified via a standalone synth) instead
of the clean `ReviewConsoleUrl` a person reading the CloudFormation console
-- or `test_agents_outputs.py::test_outputs_complete`'s exact-key
assertion, taken directly from this task's own brief -- expects. Output
names are user-facing UI, not an internal implementation detail the way a
`Runtime{Agent}` resource's own logical id is; `add_outputs(self, ...)`
called from `stack.py`'s `__init__` (scope=`self`, the stack) is what keeps
them clean.

  * `ReviewConsoleUrl` -- the HITL review console itself (merge proposals,
    resolve drift, run workflows): the gate API's own `/ui` route
    (`GateService.http_api`, `stack.py`'s `console_url` -- the SAME value
    already used to set the Cognito console client's `CallbackURLs`/
    `LogoutURLs`, so this output and that redirect target can never drift
    apart).
  * `CognitoLoginUrl` -- the hosted-UI sign-in page for the seeded admin
    user (`AdminEmail`), built via `UserPoolDomain.sign_in_url` (the L2's
    own purpose-built helper for this exact URL shape) rather than this
    module hand-assembling `https://{domain}.auth.{region}.amazoncognito.
    com/login?client_id=...&response_type=code&redirect_uri=...` itself.
  * `ApiEndpoint` -- the bare gate API base URL (no `/ui`) -- the
    programmatic endpoint a script or CI job would call, as distinct from
    `ReviewConsoleUrl`'s human-facing route.
  * `McpEndpoint` -- `services.mcp_url`, labeled internal-only in its own
    description: this is the internal ALB `services.py` builds with
    `internet_facing=False` (`Scheme=internal`) -- reachable only from
    inside the stack's VPC (the gate Lambda, the AgentCore Gateway), never
    from a browser. Printed anyway because it is useful for an operator
    debugging from a bastion/VPN inside the same VPC.
  * `FirstStepsUrl` -- a fixed link to the launch-day walkthrough
    (`docs/13-launch-stack.md`), the actual next step after this stack's
    resources finish creating.
"""

from __future__ import annotations

import aws_cdk as cdk

from fde_cdk.gate import GateService
from fde_cdk.identity import Identity
from fde_cdk.ops import OpsLayer
from fde_cdk.services import Services

# The brief's own literal -- matches the `Repository`/`Changelog` project
# URLs already checked into the repo root `pyproject.toml`.
FIRST_STEPS_URL = (
    "https://github.com/khalaharvi/FDE-Agent-Platform/blob/main/docs/13-launch-stack.md"
)


def add_outputs(
    stack: cdk.Stack,
    *,
    identity: Identity,
    gate_service: GateService,
    services: Services,
    ops: OpsLayer,
    console_url: str,
) -> None:
    cdk.CfnOutput(
        stack,
        "ReviewConsoleUrl",
        value=console_url,
        description=(
            "The HITL review console: merge proposals, resolve drift, "
            "run workflows. Sign in via CognitoLoginUrl first."
        ),
    )

    login_url = identity.hosted_ui_domain.sign_in_url(
        identity.console_client,
        redirect_uri=console_url,
    )
    cdk.CfnOutput(
        stack,
        "CognitoLoginUrl",
        value=login_url,
        description=(
            "Cognito hosted-UI sign-in page for the seeded admin user "
            "(AdminEmail) -- check that inbox for the temporary password. "
            "Signing in here proves the account works but does NOT by "
            "itself grant ReviewConsoleUrl a working session (fde_gate has "
            "no server-side OAuth code-exchange route yet) -- see docs/13 "
            "§3 for the scripted-token path this stack supports today."
        ),
    )

    cdk.CfnOutput(
        stack,
        "ApiEndpoint",
        value=gate_service.http_api.api_endpoint,
        description="The fde-gate-service HTTP API base URL (review/merge/workflow endpoints).",
    )

    cdk.CfnOutput(
        stack,
        "McpEndpoint",
        value=services.mcp_url,
        description=(
            "Internal-only: the fde-mcp server's internal ALB endpoint. "
            "Reachable only from inside this stack's VPC (the gate Lambda, "
            "the AgentCore Gateway) -- not internet-routable, not reachable "
            "from a browser."
        ),
    )

    cdk.CfnOutput(
        stack,
        "FirstStepsUrl",
        value=FIRST_STEPS_URL,
        description="Launch-day walkthrough: seed data, first login, first proposal.",
    )

    # I5 fix (final-fix-report.md): these two used to be UNCONDITIONED
    # `CfnOutput`s carrying an Fn::If-wrapped VALUE that resolved to ""
    # when OpsMode=off (see git history for the prior `ops.py` shape) --
    # which is exactly the pattern cfn-lint/CDK's own synth validation
    # report flags as W1001 ("Reference to '...' which is conditional on
    # 'OpsEnabled' - target may not exist... Add a Condition to the output
    # that implies the target's condition"). `ops.topic_arn`/
    # `ops.dashboard_url` are now the REAL, unwrapped values (a plain
    # `Fn::GetAtt`/`Fn::Join`, no `Fn::If`), and `condition=ops.
    # enabled_condition` on the `CfnOutput` itself is what CloudFormation's
    # own docs recommend for exactly this case: an Output with a Condition
    # is entirely omitted from the stack's output list (not merely blank)
    # when that condition is false, and referencing a same-condition'd
    # resource from a same-condition'd Output is structurally valid
    # (no Fn::If needed) the same way any other equally-conditioned pair
    # of resources may reference each other directly.
    cdk.CfnOutput(
        stack,
        "OpsTopicArn",
        value=ops.topic_arn,
        condition=ops.enabled_condition,
        description=(
            "The fde-ops SNS topic -- the integration seam. Subscribe your own "
            "Datadog/PagerDuty/SIEM here; set OpsMode=topic-only to skip the "
            "default email subscription. Absent from Outputs entirely when "
            "OpsMode=off."
        ),
    )
    cdk.CfnOutput(
        stack,
        "OpsDashboardUrl",
        value=ops.dashboard_url,
        condition=ops.enabled_condition,
        description=(
            "The FdeOps CloudWatch dashboard. Absent from Outputs entirely when OpsMode=off."
        ),
    )
