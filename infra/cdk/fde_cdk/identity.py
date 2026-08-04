"""Cognito identity: one user pool for every human and machine principal in
the platform.

Three app clients, one pool:

  1. `console_client` -- the review console (`fde-gate` dev/prod server,
     docs/07's HITL gate UI). Humans sign in through the Cognito Hosted UI
     using the OAuth authorization-code grant; there is no password grant
     and no implicit grant (docs/09's honesty rule: nothing here has run
     against live AWS, but the authorization-code grant is the only one
     Cognito's own guidance treats as safe for a server-rendered app that
     can hold a client secret).
  2. `api_client` -- the audience `fde_gate`'s `apigatewayv2.HttpApi` JWT
     authorizer checks against (wired in Task 6). It exists purely to give
     the API a stable audience id; it carries no OAuth flows of its own.
  3. `m2m_client` -- machine-to-machine. The AgentCore Gateway's
     `CUSTOM_JWT` authorizer (`fde_agents/deploy/gateway.py`,
     `authorizerType="CUSTOM_JWT"`) accepts tokens minted through the OAuth
     client-credentials grant, scoped to the `gateway/invoke` resource-
     server scope this construct also defines -- an agent runtime never
     signs in as a human, it exchanges its own client id/secret for a
     token scoped to exactly "call the gateway", nothing else.

Self-signup is OFF for the whole pool (`self_sign_up_enabled=False`,
i.e. `AdminCreateUserConfig.AllowAdminCreateUserOnly=True`): every user is
either the seeded admin (below) or created by that admin through the
console later. A launch-button demo stack that let anyone self-register
into the review console would let a stranger grant themselves merge
authority over `hitl.merge_proposal` -- exactly the invariant CLAUDE.md
says CI holds the line on at the database layer; not letting arbitrary
signups reach the console at all is the identity-layer half of that same
posture.
"""

from __future__ import annotations

import aws_cdk as cdk
import aws_cdk.aws_cognito as cognito
from constructs import Construct

from fde_cdk.params import LaunchParams

# The one scope this platform's Gateway authorizer checks for on an M2M
# token (`fde_agents/deploy/gateway.py`'s `create_gateway`,
# `allowedAudience`/`allowedClients` on the CUSTOM_JWT authorizer -- the
# scope itself is enforced by the Gateway's tool-invocation policy, not
# shown in that file, but the resource-server/scope pairing here is what
# lets a client-credentials token even be minted with it in the first
# place).
GATEWAY_RESOURCE_SERVER_ID = "gateway"
GATEWAY_INVOKE_SCOPE_NAME = "invoke"
GATEWAY_INVOKE_SCOPE_DESCRIPTION = "Call the FDE knowledge-graph MCP Gateway as an agent runtime."


class Identity(Construct):
    """One Cognito user pool: an admin-seeded human login (`console_client`,
    hosted UI, authorization-code grant), a stable JWT audience for the gate
    API (`api_client`), and a client-credentials M2M client scoped to
    `gateway/invoke` for agent-to-Gateway auth (`m2m_client`)."""

    user_pool: cognito.UserPool
    console_client: cognito.UserPoolClient
    api_client: cognito.UserPoolClient
    m2m_client: cognito.UserPoolClient
    discovery_url: str
    hosted_ui_domain: cognito.UserPoolDomain
    # I4(c) fix (final-fix-report.md): `m2m_client`'s generated secret,
    # retrieved WITHOUT CDK's asset-publishing machinery -- see the
    # assignment below for why the L2's own `user_pool_client_secret`
    # convenience property cannot be used in this bootstrap-free stack.
    m2m_client_secret: cdk.SecretValue

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        params: LaunchParams,
    ) -> None:
        super().__init__(scope, construct_id)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True, username=False),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # RETAIN, not the CDK default (DESTROY): matching Database's own
            # "data outlives the stack" posture (`database.py`'s
            # `RemovalPolicy.SNAPSHOT`) -- an accidental `cdk destroy`/stack
            # deletion should not silently delete every human account this
            # platform's review console depends on to enforce the merge
            # invariant.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # Seed the admin user from AdminEmail (LaunchParams -- required, no
        # default, see params.py). This is an L1 (`CfnUserPoolUser`), not
        # the higher-level `UserPool.add_client`-style helper, because CDK's
        # L2 has no "create a user" API at all -- user creation is a plain
        # data-plane operation CloudFormation models as its own resource
        # type. `username=` is `Ref`'d from the CfnParameter's
        # `.value_as_string`, never a literal, so this stays a per-launch
        # value rather than something baked into the template at synth
        # time. `desired_delivery_mediums=["EMAIL"]` is what makes Cognito
        # actually send the seeded temporary-password email -- without it
        # the user exists but the admin has no way to learn their password.
        cognito.CfnUserPoolUser(
            self,
            "AdminUser",
            user_pool_id=self.user_pool.user_pool_id,
            username=params.admin_email.value_as_string,
            desired_delivery_mediums=["EMAIL"],
            user_attributes=[
                cognito.CfnUserPoolUser.AttributeTypeProperty(
                    name="email", value=params.admin_email.value_as_string
                ),
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="email_verified", value="true"),
            ],
        )

        # Hosted UI domain: `fde-<stack-id-suffix>`. Cognito domain prefixes
        # are a GLOBAL namespace across every AWS account in the region, so
        # a fixed literal (e.g. "fde") would collide the second launch. The
        # stack id ARN is `arn:aws:cloudformation:{region}:{account}:stack/
        # {stack-name}/{unique-suffix}` -- `Fn::Select(2, Fn::Split("/",
        # stack_id))` pulls out just the unique (GUID-shaped) suffix, which
        # is unique per *stack instance*, not just per template, so two
        # people launching this same template into the same account/region
        # still get two different domains.
        stack_id_suffix = cdk.Fn.select(2, cdk.Fn.split("/", cdk.Aws.STACK_ID))
        domain_prefix = cdk.Fn.join("-", ["fde", stack_id_suffix])
        # Stored (Task 7), not just constructed inline: `outputs.py`'s
        # `CognitoLoginUrl` needs the domain object itself to call its own
        # `sign_in_url(client, redirect_uri=...)` helper (the L2's
        # purpose-built way to build a hosted-UI login URL, rather than
        # this stack hand-assembling the same `https://{domain}.auth.
        # {region}.amazoncognito.com/login?...` shape a second time).
        self.hosted_ui_domain = cognito.UserPoolDomain(
            self,
            "HostedUiDomain",
            user_pool=self.user_pool,
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )

        # Resource server + scope the m2m_client's client-credentials grant
        # is minted against. `identifier="gateway"` + `scope_name="invoke"`
        # is what produces the scope string Cognito emits into the token as
        # "gateway/invoke" (ResourceServerIdentifier/ScopeName joined by
        # "/" -- Cognito's own convention, not this construct's).
        resource_server = cognito.UserPoolResourceServer(
            self,
            "GatewayResourceServer",
            user_pool=self.user_pool,
            identifier=GATEWAY_RESOURCE_SERVER_ID,
            scopes=[
                cognito.ResourceServerScope(
                    scope_name=GATEWAY_INVOKE_SCOPE_NAME,
                    scope_description=GATEWAY_INVOKE_SCOPE_DESCRIPTION,
                )
            ],
        )

        # No `callback_urls=` override: the review console's real URL isn't
        # known to this construct (it depends on the gate API's domain,
        # which `GateService`, Task 6, builds). CDK fills the
        # CloudFormation-required `CallbackURLs` list with its own default
        # (`https://example.com`) when authorization-code-grant flows are
        # enabled and none is given. `FdePlatformStack.__init__` (stack.py)
        # replaces that placeholder once `GateService.http_api` exists, via
        # `add_property_override("CallbackURLs"/"LogoutURLs", ...)` on this
        # client's L1 -- there is no L2 setter for either property
        # post-construction, so the override is the only mechanism.
        self.console_client = cognito.UserPoolClient(
            self,
            "ConsoleClient",
            user_pool=self.user_pool,
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
            ),
            prevent_user_existence_errors=True,
        )

        # No OAuth flows: this client exists only so its client id can be
        # the JWT authorizer's `AllowedAudience` for the gate API's
        # apigatewayv2.HttpApi (Task 6) -- it never itself requests a
        # token.
        self.api_client = cognito.UserPoolClient(
            self,
            "ApiClient",
            user_pool=self.user_pool,
            generate_secret=False,
            disable_o_auth=True,
            prevent_user_existence_errors=True,
        )

        self.m2m_client = cognito.UserPoolClient(
            self,
            "M2mClient",
            user_pool=self.user_pool,
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[
                    cognito.OAuthScope.resource_server(
                        resource_server,
                        cognito.ResourceServerScope(
                            scope_name=GATEWAY_INVOKE_SCOPE_NAME,
                            scope_description=GATEWAY_INVOKE_SCOPE_DESCRIPTION,
                        ),
                    )
                ],
            ),
            prevent_user_existence_errors=True,
        )

        # Plain Python string interpolation over CDK tokens, not an
        # `Fn::Sub`: `cdk.Aws.REGION` and `user_pool_id` are both CDK
        # "string tokens" -- ordinary Python `str` values carrying an
        # embedded, still-unresolved marker -- and CDK's own synth-time
        # resolver (`Stack.resolve`) walks every string in the tree looking
        # for those markers, splicing in the real `Fn::Join`/`Ref` wherever
        # it finds one. An f-string over tokens is exactly the CDK-endorsed
        # way to build a composite string; it is not resolved to a literal
        # until deploy time, same as if this were built with `Fn.join`.
        self.discovery_url = (
            f"https://cognito-idp.{cdk.Aws.REGION}.amazonaws.com/"
            f"{self.user_pool.user_pool_id}/.well-known/openid-configuration"
        )

        # I4(c) fix (final-fix-report.md): `UserPoolClient.
        # user_pool_client_secret` (the L2 convenience property) resolves
        # the secret via an `AwsCustomResource` (a `DescribeUserPoolClient`
        # call at deploy time) -- which needs CDK's asset-publishing
        # Provider framework, forbidden by this stack's bootstrap-free
        # contract (`BootstraplessSynthesizer`, `stack.py`). Confirmed
        # empirically: calling that property here raises
        # `CannotAddAssetsStackUses` at synth time. `CfnUserPoolClient.
        # attr_client_secret` is the identical value via a genuine native
        # CloudFormation `Fn::GetAtt` (`AWS::Cognito::UserPoolClient`'s own
        # resource-provider schema declares `ClientSecret` as a returned
        # attribute) -- no asset, no custom resource, verified via a
        # standalone synth. `SecretValue.resource_attribute(...)` is CDK's
        # documented (non-"unsafe") wrapper for exactly this shape: a
        # string token that is itself a resource-attribute reference, not
        # literal plaintext -- used by `agents.py`'s `Agents` construct to
        # provision the `fde-gateway-m2m` AgentCore Identity OAuth2
        # credential provider.
        cfn_m2m_client = self.m2m_client.node.default_child
        assert cfn_m2m_client is not None
        self.m2m_client_secret = cdk.SecretValue.resource_attribute(
            cfn_m2m_client.attr_client_secret
        )
