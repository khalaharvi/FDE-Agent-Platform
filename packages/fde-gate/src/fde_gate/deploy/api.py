"""Create the API Gateway HTTP API in front of the gate service.

HTTP API (v2), not REST API (v1). Three reasons, in order of how much they
matter here:

1. **Native JWT authorizer.** A REST API would need a Lambda authorizer --
   another function, another cold start, another thing holding the JWKS
   cache. The HTTP API verifies the token itself against the issuer's
   discovery document, which is the same CUSTOM_JWT arrangement
   `fde_agents.deploy.gateway` uses for the Gateway.
2. **Payload format 2.0**, which `fde_gate.http.parse_apigw_event` is
   written against.
3. It is roughly a third the price for the same traffic.

Two routes, not eighteen
-------------------------
`ANY /{proxy+}` behind the authorizer, plus `GET /healthz` with no
authorizer. The router inside the Lambda already owns the URL space, and
mirroring all eighteen routes into API Gateway would mean every new endpoint
needed a deploy of BOTH -- with the failure mode being a 404 from the edge
that looks nothing like a routing bug in the service.

`/healthz` is the one exception because a health check that needs a valid
JWT is a health check nothing can call.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import boto3

from fde_mcp.logging import get_logger

log = get_logger(__name__)

DEFAULT_API_NAME = "fde-gate-api"
DEFAULT_FUNCTION_NAME = "fde-gate-service"


def _function_arn(lambda_client: Any, function_name: str) -> str:
    return str(
        lambda_client.get_function(FunctionName=function_name)["Configuration"]["FunctionArn"]
    )


def create_api(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    log.info("gate_api_creating", api_name=args.api_name)
    return client.create_api(  # type: ignore[no-any-return]
        Name=args.api_name,
        ProtocolType="HTTP",
        Description="FDE gate service: review API and prod-ops console",
        # No CORS configuration at all. The console is served by the same
        # origin as the API, and every other client is server-side. A
        # permissive CORS policy here would be the only thing standing
        # between a reviewer's browser session and any page on the internet.
    )


def create_authorizer(client: Any, api_id: str, args: argparse.Namespace) -> str:
    log.info("gate_api_authorizer_creating", api_id=api_id, issuer=args.jwt_issuer)
    response = client.create_authorizer(
        ApiId=api_id,
        AuthorizerType="JWT",
        Name="fde-gate-jwt",
        IdentitySource=["$request.header.Authorization"],
        JwtConfiguration={"Issuer": args.jwt_issuer, "Audience": args.jwt_audience},
    )
    return str(response["AuthorizerId"])


def create_integration(client: Any, api_id: str, function_arn: str) -> str:
    response = client.create_integration(
        ApiId=api_id,
        IntegrationType="AWS_PROXY",
        IntegrationUri=function_arn,
        PayloadFormatVersion="2.0",
        # The Lambda's own 900s timeout is the real ceiling; this is API
        # Gateway's hard 30s maximum for a synchronous request, which is why
        # the runner hands slow steps to an async invoke instead.
        TimeoutInMillis=30000,
    )
    return str(response["IntegrationId"])


def create_routes(
    client: Any, api_id: str, integration_id: str, authorizer_id: str
) -> list[dict[str, Any]]:
    target = f"integrations/{integration_id}"
    routes = [
        client.create_route(
            ApiId=api_id,
            RouteKey="ANY /{proxy+}",
            Target=target,
            AuthorizationType="JWT",
            AuthorizerId=authorizer_id,
        ),
        client.create_route(
            ApiId=api_id,
            RouteKey="GET /healthz",
            Target=target,
            AuthorizationType="NONE",
        ),
    ]
    return [dict(route) for route in routes]


def create_stage(client: Any, api_id: str) -> dict[str, Any]:
    """The `$default` stage, auto-deploying.

    `$default` means paths have no stage prefix, so `/ui/proposals/12` is the
    real URL -- which matters because the console emits absolute paths in its
    redirects and form actions, and a `/prod` prefix would break every one of
    them.
    """
    return client.create_stage(  # type: ignore[no-any-return]
        ApiId=api_id, StageName="$default", AutoDeploy=True
    )


def add_invoke_permission(
    lambda_client: Any, function_name: str, api_id: str, account_id: str, region: str
) -> None:
    """Let this ONE api invoke the function.

    Scoped by source ARN rather than granting `apigateway.amazonaws.com`
    broadly: without the condition, any API Gateway in any account could
    invoke a function that can merge into the knowledge graph.
    """
    lambda_client.add_permission(
        FunctionName=function_name,
        StatementId=f"apigw-{api_id}",
        Action="lambda:InvokeFunction",
        Principal="apigateway.amazonaws.com",
        SourceArn=f"arn:aws:execute-api:{region}:{account_id}:{api_id}/*/*",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-name", default=DEFAULT_API_NAME)
    p.add_argument("--function-name", default=DEFAULT_FUNCTION_NAME)
    p.add_argument(
        "--jwt-issuer",
        required=True,
        default=os.environ.get("FDE_JWT_ISSUER"),
        help="e.g. https://cognito-idp.us-west-2.amazonaws.com/<user-pool-id>",
    )
    p.add_argument(
        "--jwt-audience",
        nargs="+",
        required=True,
        help="accepted `aud` values (the Cognito app client id(s))",
    )
    p.add_argument("--region", dest="aws_region", default=os.environ.get("AWS_REGION"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    region = args.aws_region or boto3.session.Session().region_name
    if not region:
        print("no region: pass --region or set AWS_REGION", file=sys.stderr)
        return 2

    apigw = boto3.client("apigatewayv2", region_name=region)
    lambda_client = boto3.client("lambda", region_name=region)
    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]

    try:
        function_arn = _function_arn(lambda_client, args.function_name)
        api = create_api(apigw, args)
        api_id = str(api["ApiId"])
        authorizer_id = create_authorizer(apigw, api_id, args)
        integration_id = create_integration(apigw, api_id, function_arn)
        create_routes(apigw, api_id, integration_id, authorizer_id)
        create_stage(apigw, api_id)
        add_invoke_permission(lambda_client, args.function_name, api_id, account_id, region)
    except Exception:
        log.exception("gate_api_deploy_failed", api_name=args.api_name)
        return 1

    endpoint = api.get("ApiEndpoint")
    print(f"api id:   {api_id}")
    print(f"endpoint: {endpoint}")
    print(f"console:  {endpoint}/ui")
    print(f"health:   {endpoint}/healthz  (no auth)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
