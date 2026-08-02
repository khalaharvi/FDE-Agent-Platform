"""Provision AgentCore Runtimes for the Engagement, Workflow, and
Development agents.

Two artifact shapes are supported, matching `bedrock-agentcore-control`'s
`CreateAgentRuntime.agentRuntimeArtifact` union exactly:

  * **Container** -- `{'containerConfiguration': {'containerUri': ...}}`.
    The normal path once each agent's own container image has been built
    and pushed to ECR.
  * **Code** -- `{'codeConfiguration': {'code': {'s3': {'bucket',
    'prefix'}}, 'runtime': 'PYTHON_3_12', 'entryPoint': ['agent.py']}}`.
    AgentCore builds and runs the code directly from an S3 prefix; useful
    for a fast dev loop that skips the container build/push step entirely.
    `entryPoint` is `agent.py` alone (not a package-qualified path) because
    the S3 prefix for each agent is expected to contain that agent's own
    packaged contents flattened to the prefix root.

Both shapes are driven by ONE call site (`_build_artifact`) so a deployment
can be flipped between them with a single environment variable
(`FDE_DEPLOY_ARTIFACT_MODE=container|code`) without touching the rest of
this script.

This script is idempotent in the boring way: it does not check whether a
runtime with the same name already exists before creating one. Re-running
it against an already-provisioned environment will fail with a name
collision from the API, which is the correct behaviour for a provisioning
script that should never silently double-create a security-relevant
resource -- use `--update` (which calls `UpdateAgentRuntime` instead) for
redeploying an existing runtime with a new artifact.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import boto3

from fde_agents.common.config import DEFAULT_MODEL_ID, MODEL_PRESETS
from fde_mcp.logging import get_logger

log = get_logger(__name__)

AGENT_NAMES = ("engagement", "workflow", "development")

# Idle-session and max-lifetime ceilings match the verified AgentCore facts
# this platform is built against: 15-minute idle reap, 8-hour max session.
# These are enforced by the platform regardless of what an agent's own HITL
# wait (fde_agents.common.hitl) thinks its timeout should be -- the runtime
# will tear down a session at maxLifetime even mid-wait, which is exactly
# why that module's docstring tells callers to keep their own timeout well
# inside this ceiling rather than trying to span it.
DEFAULT_IDLE_TIMEOUT_S = 900
DEFAULT_MAX_LIFETIME_S = 28800


def _build_artifact(agent_name: str, args: argparse.Namespace) -> dict[str, Any]:
    if args.artifact_mode == "container":
        registry = args.ecr_registry or os.environ["FDE_ECR_REGISTRY"]
        tag = args.image_tag or os.environ.get("FDE_IMAGE_TAG", "latest")
        # `fde-{agent}`, NOT `fde-{agent}-agent`: the Dockerfile, the CI image
        # build (.github/workflows/ci.yml), and the README all name the
        # repository that way, and a mismatch here is only discoverable as an
        # ImageNotFound at runtime-creation time.
        container_uri = f"{registry}/fde-{agent_name}:{tag}"
        return {"containerConfiguration": {"containerUri": container_uri}}

    if args.artifact_mode == "code":
        bucket = args.code_bucket or os.environ["FDE_CODE_BUCKET"]
        prefix = f"{args.code_prefix_root.rstrip('/')}/{agent_name}"
        return {
            "codeConfiguration": {
                "code": {"s3": {"bucket": bucket, "prefix": prefix}},
                "runtime": "PYTHON_3_12",
                "entryPoint": ["agent.py"],
            }
        }

    msg = f"unknown artifact mode {args.artifact_mode!r}"
    raise ValueError(msg)


def _authorizer_configuration(args: argparse.Namespace) -> dict[str, Any] | None:
    """Gate the runtime's own inbound auth behind the same CUSTOM_JWT scheme
    the Gateway uses (see `gateway.py`), when a discovery URL is given.
    Omitted entirely (AgentCore's default IAM-only auth applies) if
    `--jwt-discovery-url` is not passed -- most deployments front these
    runtimes exclusively through the Gateway and never invoke them directly,
    so a JWT authorizer on the runtime itself is opt-in, not assumed.
    """
    if not args.jwt_discovery_url:
        return None
    return {
        "customJWTAuthorizer": {
            "discoveryUrl": args.jwt_discovery_url,
            "allowedAudience": args.jwt_allowed_audience or None,
            "allowedClients": args.jwt_allowed_clients or None,
        }
    }


def _resolved_model_id(agent_name: str, args: argparse.Namespace) -> str:
    """Same resolution order as the runtime's own config.resolve_model_id:
    explicit --model-id / FDE_MODEL_ID > --model-preset per agent >
    DEFAULT_MODEL_ID. Resolved HERE so every provisioned runtime carries a
    concrete FDE_MODEL_ID and the audit trail records exactly what runs.
    """
    if args.model_id:
        return str(args.model_id)
    if args.model_preset:
        return MODEL_PRESETS[args.model_preset][agent_name]
    return DEFAULT_MODEL_ID


def _environment_variables(agent_name: str, args: argparse.Namespace) -> dict[str, str]:
    env = {
        "FDE_AGENT_NAME": agent_name,
        "FDE_MODEL_ID": _resolved_model_id(agent_name, args),
    }
    if args.gateway_url:
        env["FDE_GATEWAY_URL"] = args.gateway_url
    if args.db_secret_arn:
        env["FDE_DB_SECRET_ARN"] = args.db_secret_arn
    if args.aws_region:
        env["AWS_REGION"] = args.aws_region
    return env


def create_runtime(client: Any, agent_name: str, args: argparse.Namespace) -> dict[str, Any]:
    artifact = _build_artifact(agent_name, args)
    authorizer = _authorizer_configuration(args)

    kwargs: dict[str, Any] = {
        "agentRuntimeName": f"fde-{agent_name}-agent",
        "agentRuntimeArtifact": artifact,
        "roleArn": args.role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": args.idle_timeout_s,
            "maxLifetime": args.max_lifetime_s,
        },
        "environmentVariables": _environment_variables(agent_name, args),
        "description": f"FDE Platform {agent_name} agent",
    }
    if authorizer:
        kwargs["authorizerConfiguration"] = authorizer

    log.info("agent_runtime_creating", agent_name=agent_name, artifact_mode=args.artifact_mode)
    response = client.create_agent_runtime(**kwargs)
    log.info(
        "agent_runtime_created",
        agent_name=agent_name,
        agent_runtime_arn=response.get("agentRuntimeArn"),
        status=response.get("status"),
    )
    return response  # type: ignore[no-any-return]


def update_runtime(
    client: Any, agent_name: str, agent_runtime_id: str, args: argparse.Namespace
) -> dict[str, Any]:
    artifact = _build_artifact(agent_name, args)
    kwargs: dict[str, Any] = {
        "agentRuntimeId": agent_runtime_id,
        "agentRuntimeArtifact": artifact,
        "roleArn": args.role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": args.idle_timeout_s,
            "maxLifetime": args.max_lifetime_s,
        },
        "environmentVariables": _environment_variables(agent_name, args),
    }
    log.info("agent_runtime_updating", agent_runtime_id=agent_runtime_id, agent_name=agent_name)
    response = client.update_agent_runtime(**kwargs)
    log.info(
        "agent_runtime_updated", agent_runtime_id=agent_runtime_id, status=response.get("status")
    )
    return response  # type: ignore[no-any-return]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agents", nargs="+", default=list(AGENT_NAMES), choices=AGENT_NAMES)
    p.add_argument(
        "--artifact-mode",
        choices=["container", "code"],
        default=os.environ.get("FDE_DEPLOY_ARTIFACT_MODE", "container"),
    )
    p.add_argument("--role-arn", required=True, help="Runtime execution role ARN (see deploy/iam/)")
    p.add_argument("--region", dest="aws_region", default=os.environ.get("AWS_REGION"))
    p.add_argument(
        "--model-id",
        default=os.environ.get("FDE_MODEL_ID"),
        help="explicit Bedrock model id for ALL agents; wins over --model-preset "
        "(same resolution order the runtime itself uses)",
    )
    p.add_argument(
        "--model-preset",
        choices=sorted(MODEL_PRESETS),
        default=os.environ.get("FDE_MODEL_PRESET"),
        help="per-agent model tier (see common/config.py MODEL_PRESETS and the "
        "README cost table); resolved to a concrete FDE_MODEL_ID per runtime here, "
        "at provisioning time",
    )
    p.add_argument("--gateway-url", default=os.environ.get("FDE_GATEWAY_URL"))
    p.add_argument("--db-secret-arn", default=os.environ.get("FDE_DB_SECRET_ARN"))
    p.add_argument("--idle-timeout-s", type=int, default=DEFAULT_IDLE_TIMEOUT_S)
    p.add_argument("--max-lifetime-s", type=int, default=DEFAULT_MAX_LIFETIME_S)
    p.add_argument("--ecr-registry", default=os.environ.get("FDE_ECR_REGISTRY"))
    p.add_argument("--image-tag", default=os.environ.get("FDE_IMAGE_TAG", "latest"))
    p.add_argument("--code-bucket", default=os.environ.get("FDE_CODE_BUCKET"))
    p.add_argument(
        "--code-prefix-root", default=os.environ.get("FDE_CODE_PREFIX_ROOT", "fde-agents")
    )
    p.add_argument("--jwt-discovery-url", default=os.environ.get("FDE_JWT_DISCOVERY_URL"))
    p.add_argument("--jwt-allowed-audience", nargs="*", default=None)
    p.add_argument("--jwt-allowed-clients", nargs="*", default=None)
    p.add_argument(
        "--update",
        metavar="AGENT_RUNTIME_ID",
        help="Update this existing runtime instead of creating a new one (only valid with a single --agents value)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    client = boto3.client("bedrock-agentcore-control", region_name=args.aws_region)

    if args.update:
        if len(args.agents) != 1:
            log.error("update_requires_single_agent", agents=args.agents)
            return 2
        update_runtime(client, args.agents[0], args.update, args)
        return 0

    results = []
    for agent_name in args.agents:
        try:
            results.append(create_runtime(client, agent_name, args))
        except Exception:
            log.exception("agent_runtime_create_failed", agent_name=agent_name)
            return 1
    for agent_name, result in zip(args.agents, results, strict=True):
        print(f"{agent_name}: {result.get('agentRuntimeArn')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
