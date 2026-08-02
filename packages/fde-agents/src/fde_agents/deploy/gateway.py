"""Provision the AgentCore Gateway that fronts the FDE knowledge-graph MCP
server for all three agent runtimes, plus its two targets.

Why a Gateway at all, given `fde_mcp.server` already speaks streamable-HTTP
directly: the Gateway is what lets the Engagement, Workflow, and
Development runtimes share ONE deployed copy of the graph tool surface with
centralized auth (`CUSTOM_JWT`), rather than each runtime needing its own
direct network path to the MCP server container (see `fde_mcp`'s README's
"Mapping to AgentCore: Gateway vs. in-runtime MCP" section -- this script
provisions the Gateway half of that split; the in-runtime/stdio half needs
no provisioning at all, it is just a subprocess).

Two targets are created:

  1. **`mcpServer` target** -- points at the deployed `fde_mcp.server`
     streamable-HTTP endpoint itself. This is the FDE knowledge-graph tool
     surface (`kg_*`, `hitl`/proposal, drift, workflow tools) an agent
     actually calls day to day.
  2. **`lambda` target** -- a narrow escape hatch for tools that must run
     as a Lambda rather than as a call into the always-on MCP server
     process, e.g. a heavyweight one-off batch operation (a full
     `sor.observation` backfill from a customer's data warehouse) that
     should not tie up the MCP server's connection pool
     (`FDE_DB_POOL_MAX`, see `fde_mcp.db`) for the minutes or hours it might
     run. Its tool schema is supplied inline (`toolSchema.inlinePayload`)
     rather than fetched from S3, since this platform's Lambda-backed tools
     are few and change rarely enough that keeping the schema versioned
     alongside the code is simpler than another S3 object to keep in sync.

     That schema is `deploy/schemas/sor_backfill_tool_schema.json`, loaded
     via `importlib.resources` so it travels with the installed package.
     It used to be a placeholder dict written inline in this function, and
     it had drifted: it described a tool that "returns immediately with a
     job id", which `fde_sor.lambda_handlers.backfill_handler` does not and
     never did. Nothing connected the two, so nothing caught it. The schema
     is now the real handler's contract, and
     `packages/fde-sor/tests/test_gateway_schema.py` fails if the two stop
     agreeing. `--lambda-tool-schema-file` still overrides it, for a
     deployment that registers additional Lambda-backed tools.

`protocolType='MCP'` with `protocolConfiguration.mcp.searchType='SEMANTIC'`
is what lets the Gateway's own tool search narrow an agent's tool list by
semantic relevance to the current task rather than the agent needing every
tool name hard-coded -- useful headroom for when this platform's tool
surface grows past what fits comfortably in one system prompt's tool list.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import boto3

from fde_mcp.logging import get_logger

log = get_logger(__name__)

DEFAULT_LAMBDA_TOOL_SCHEMA = "sor_backfill_tool_schema.json"


def load_default_tool_schema() -> list[dict[str, Any]]:
    """The packaged `lambda` target tool schema.

    `importlib.resources` rather than a path relative to `__file__` so it
    resolves from an installed wheel and from a zipped code artifact, not only
    from a source checkout -- `deploy/codezip.py` builds the latter.
    """
    text = (
        resources.files("fde_agents.deploy.schemas")
        .joinpath(DEFAULT_LAMBDA_TOOL_SCHEMA)
        .read_text(encoding="utf-8")
    )
    schema: list[dict[str, Any]] = json.loads(text)
    return schema


def create_gateway(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    response = client.create_gateway(
        name=args.gateway_name,
        description="FDE Platform knowledge-graph MCP Gateway",
        roleArn=args.role_arn,
        protocolType="MCP",
        protocolConfiguration={
            "mcp": {
                "searchType": "SEMANTIC",
                "supportedVersions": ["2025-06-18"],
                "instructions": (
                    "Tools over the FDE knowledge graph: bitemporal process/role/system "
                    "graph, hybrid ANN+graph retrieval, human-in-the-loop change "
                    "proposals, drift monitoring, and faithful workflow authoring. "
                    "Call kg_head_commit first and pin its commit_id for the task."
                ),
            }
        },
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": args.jwt_discovery_url,
                "allowedAudience": args.jwt_allowed_audience,
                "allowedClients": args.jwt_allowed_clients,
            }
        },
    )
    log.info(
        "gateway_created",
        gateway_name=args.gateway_name,
        gateway_id=response.get("gatewayId"),
        gateway_url=response.get("gatewayUrl"),
    )
    return response  # type: ignore[no-any-return]


def create_mcp_server_target(
    client: Any, gateway_id: str, args: argparse.Namespace
) -> dict[str, Any]:
    """Register `fde_mcp.server`'s deployed streamable-HTTP endpoint
    (`FDE_MCP_TRANSPORT=http`, per its own module docstring) as an
    `mcpServer` target -- the Gateway proxies MCP protocol traffic straight
    through to it rather than re-implementing the graph tool surface.
    """
    response = client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name="fde-kg-mcp-server",
        description="FDE knowledge-graph MCP server (fde_mcp.server, FDE_MCP_TRANSPORT=http)",
        targetConfiguration={
            "mcp": {
                "mcpServer": {
                    "endpoint": args.mcp_server_endpoint,
                    "listingMode": "DEFAULT",
                }
            }
        },
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"},
        ],
    )
    log.info("gateway_mcp_server_target_created", target_id=response.get("targetId"))
    return response  # type: ignore[no-any-return]


def create_lambda_target(
    client: Any, gateway_id: str, args: argparse.Namespace
) -> dict[str, Any] | None:
    if not args.lambda_arn:
        log.info("gateway_lambda_target_skipped", reason="no --lambda-arn given")
        return None
    tool_schema = (
        json.loads(Path(args.lambda_tool_schema_file).read_text())
        if args.lambda_tool_schema_file
        else load_default_tool_schema()
    )

    response = client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name="fde-batch-lambda",
        description="Escape hatch for long-running/batch tools that should not tie up the MCP server's connection pool",
        targetConfiguration={
            "mcp": {
                "lambda": {
                    "lambdaArn": args.lambda_arn,
                    "toolSchema": {"inlinePayload": tool_schema},
                }
            }
        },
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"},
        ],
    )
    log.info("gateway_lambda_target_created", target_id=response.get("targetId"))
    return response  # type: ignore[no-any-return]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gateway-name", default=os.environ.get("FDE_GATEWAY_NAME", "fde-kg-gateway"))
    p.add_argument("--role-arn", required=True, help="Gateway service role ARN")
    p.add_argument("--region", dest="aws_region", default=os.environ.get("AWS_REGION"))
    p.add_argument(
        "--jwt-discovery-url",
        required=True,
        help="OIDC discovery URL, e.g. Cognito user pool .well-known/openid-configuration",
    )
    p.add_argument("--jwt-allowed-audience", nargs="+", required=True)
    p.add_argument("--jwt-allowed-clients", nargs="+", default=None)
    p.add_argument(
        "--mcp-server-endpoint",
        required=True,
        help="Deployed fde_mcp.server streamable-HTTP endpoint, e.g. https://.../mcp",
    )
    p.add_argument("--lambda-arn", default=os.environ.get("FDE_BATCH_LAMBDA_ARN"))
    p.add_argument("--lambda-tool-schema-file", default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    client = boto3.client("bedrock-agentcore-control", region_name=args.aws_region)

    gateway = create_gateway(client, args)
    gateway_id = gateway["gatewayId"]

    create_mcp_server_target(client, gateway_id, args)
    create_lambda_target(client, gateway_id, args)

    print(json.dumps({"gatewayId": gateway_id, "gatewayUrl": gateway.get("gatewayUrl")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
