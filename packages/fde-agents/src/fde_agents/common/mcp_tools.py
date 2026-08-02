"""Builds the Strands MCP client the three agents use to reach the FDE
knowledge-graph tool surface (`fde_mcp.server`), plus the one sanctioned
exception to "agents only reach state through MCP tools" (`raw_sql`).

Two deployment shapes, one call site
-------------------------------------
Exactly mirroring `fde_mcp.server`'s own "two deployment shapes, one
codebase" split (see its module docstring):

* **Dev**: `GatewaySettings.url` unset. We spawn `fde_mcp`'s server as a
  local stdio subprocess (`FDE_MCP_TRANSPORT=stdio`, its default) and talk
  JSON-RPC over stdin/stdout via `mcp.client.stdio.stdio_client`. This is
  what a developer or CI running the agent loop against a local Postgres
  gets, with zero AWS dependencies.

* **Prod**: `GatewaySettings.url` is set to an AgentCore Gateway MCP
  endpoint (`https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp`).
  We connect over streamable HTTP (`mcp.client.streamable_http.
  streamablehttp_client`) and attach `Authorization: Bearer <token>`, where
  the token is minted through AgentCore Identity's machine-to-machine OAuth
  flow (`bedrock_agentcore.services.identity.IdentityClient`) rather than a
  static secret -- the Gateway's `CUSTOM_JWT` authorizer (`deploy/gateway.
  py`) validates exactly this kind of token against its configured
  discovery URL / allowed audience / allowed clients.

Callers never construct an `MCPClient` directly; they call
`build_mcp_client()` and use it as a context manager (Strands' `MCPClient`
runs its transport in a background thread so the same connection can be
reused for every tool call in a session -- see its docstring).

`raw_sql`
---------
`fde_mcp.server`'s tool surface has no `trace_step_record` tool (see its
README's tool inventory -- tracing is something the MCP server does FOR
ITSELF on every tool call via its own `emit_trace`, but nothing writes the
user/assistant turns an agent orchestrator sees before/between tool calls).
Rather than invent and ship a new MCP tool whose only job is "INSERT into a
table fde_agent can already write to" -- which would mean a full Gateway
round trip per conversational turn, purely for telemetry -- `tracing.py`
writes those two tables directly, through the identical connection contract
`fde_mcp.db` enforces (`SET LOCAL ROLE fde_agent`, per-call
statement_timeout) for every other write in this platform.

Because `fde-agents` depends on `fde-mcp` as a real installed workspace
package (see the workspace root `pyproject.toml`'s module docstring), this
is a plain import of `fde_mcp.db.tool_transaction` -- there is exactly one
implementation of "how fde_agent gets a connection" in the whole platform,
imported from two entry points, with no subprocess-adjacent module-loading
trick required to reach it.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING, Any

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

from fde_mcp import db as fde_db
from fde_mcp.config import get_settings
from fde_mcp.logging import get_logger

from .config import GatewaySettings, get_agent_runtime_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

log = get_logger(__name__)


def _mint_gateway_bearer_token(settings: GatewaySettings) -> str:
    """Acquire a bearer token for the Gateway's CUSTOM_JWT authorizer via
    AgentCore Identity's machine-to-machine flow.

    Uses `IdentityClient.get_token` directly (rather than the
    `@requires_access_token` decorator form) because we need the token
    synchronously at connection-build time, not injected into a specific
    decorated coroutine's kwargs. `auth_flow='M2M'` is the correct flow for
    an agent runtime calling the Gateway on its own behalf (no end user in
    the loop). `agent_identity_token` is the runtime's own workload identity
    token, resolved from the ambient AgentCore execution environment by the
    SDK; we do not fetch or cache it ourselves. The AWS region comes from
    `fde_mcp.config`'s already-parsed `AWS_REGION`/`AWS_DEFAULT_REGION`
    resolution (`DatabaseSettings.aws_region`) rather than a second,
    independent `os.environ` read here.

    Imports `IdentityClient` from `bedrock_agentcore.services.identity`
    (its defining module) and `BedrockAgentCoreContext` from
    `bedrock_agentcore.runtime` (which re-exports it via an explicit
    `__all__`) rather than from `bedrock_agentcore.identity.auth`, which
    re-exports both without one -- `no_implicit_reexport` (this workspace's
    mypy strict setting) treats an implicit re-export as not part of a
    module's public API, so importing from the un-declared path would be a
    type error against bedrock_agentcore's own shipped `py.typed` stubs.
    """
    # Gateway-mode-only; imported lazily so a dev-mode-only deployment never
    # needs a fully configured AgentCore identity/workload environment.
    from bedrock_agentcore.runtime import BedrockAgentCoreContext  # noqa: PLC0415
    from bedrock_agentcore.services.identity import IdentityClient  # noqa: PLC0415

    region = get_settings().db.aws_region
    if region is None:
        msg = (
            "cannot mint a Gateway bearer token: no AWS region configured "
            "(set AWS_REGION or AWS_DEFAULT_REGION)"
        )
        raise RuntimeError(msg)
    agent_identity_token = BedrockAgentCoreContext.get_workload_access_token()
    if agent_identity_token is None:
        msg = (
            "cannot mint a Gateway bearer token: no workload access token in "
            "context -- this process is not running inside an AgentCore "
            "Runtime session"
        )
        raise RuntimeError(msg)

    client = IdentityClient(region=region)
    return str(
        client.get_token(
            provider_name=settings.identity_provider_name,
            scopes=list(settings.scopes),
            agent_identity_token=agent_identity_token,
            auth_flow="M2M",
        )
    )


def _gateway_headers(settings: GatewaySettings) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint_gateway_bearer_token(settings)}"}


def _stdio_transport(settings: GatewaySettings) -> Callable[[], Any]:
    params = StdioServerParameters(
        command=settings.stdio_command,
        args=list(settings.stdio_args),
        cwd=settings.stdio_cwd,
        env={
            # Only forward what the subprocess needs; do not leak the whole
            # agent process environment (credentials for services the MCP
            # server has no business touching) into a spawned child. This is
            # a live passthrough of the CURRENT environment, not a config
            # value of our own -- it deliberately reads os.environ directly
            # rather than through a settings object.
            k: v
            for k, v in os.environ.items()
            if k.startswith("FDE_") or k in ("AWS_REGION", "AWS_DEFAULT_REGION", "PATH")
        },
    )
    return lambda: stdio_client(params)


def _gateway_transport(settings: GatewaySettings) -> Callable[[], Any]:
    if settings.url is None:
        msg = "build_mcp_client() gateway path called with no gateway URL configured"
        raise RuntimeError(msg)
    url = settings.url

    def _connect() -> Any:
        return streamablehttp_client(
            url, headers=_gateway_headers(settings), timeout=settings.http_timeout_s
        )

    return _connect


def build_mcp_client(settings: GatewaySettings | None = None) -> MCPClient:
    """Return a Strands `MCPClient` wired to whichever transport this
    environment is configured for. The caller is responsible for using it
    as a context manager:

        with build_mcp_client() as mcp_client:
            tools = mcp_client.list_tools_sync()
            agent = Agent(model=..., tools=tools, system_prompt=...)
            ...

    Streamable HTTP is the ONLY transport the Gateway supports (per the
    verified facts this module targets) -- there is deliberately no
    "gateway over stdio" branch.

    `settings` defaults to `get_agent_runtime_settings().gateway` -- passed
    explicitly here (rather than read internally on every call) so tests can
    supply a `GatewaySettings` without monkeypatching process-wide config.
    """
    resolved = settings if settings is not None else get_agent_runtime_settings().gateway
    if resolved.url:
        log.info("mcp_client_connecting", transport="gateway", url=resolved.url)
        return MCPClient(_gateway_transport(resolved), startup_timeout=resolved.startup_timeout_s)
    log.info(
        "mcp_client_connecting",
        transport="stdio",
        command=resolved.stdio_command,
        args=resolved.stdio_args,
        cwd=resolved.stdio_cwd,
    )
    return MCPClient(_stdio_transport(resolved), startup_timeout=resolved.startup_timeout_s)


async def raw_sql(mcp_client: Any, sql: str, params: dict[str, Any]) -> None:
    """Execute one write statement against `trn.trace_session`/`trn.
    trace_step` using the same role/timeout contract as every MCP tool call.

    `mcp_client` is accepted (and unused directly) so call sites read as
    "do this in the context of this agent's MCP connection" even though the
    actual DB access is a sibling connection under the same role -- kept as
    a parameter rather than dropped so a future refactor that DOES add a
    Gateway-mediated trace tool is a one-line change at the two call sites
    in `tracing.py` and `hitl.py`, not a signature change.

    Deliberately restricted to `tracing.py`, the only caller in this
    package, which only ever targets `trn.*` tables the `fde_agent` role
    already has INSERT on (`db/010_roles_and_seed_policy.sql`) -- this
    function does not, and must not, become a general SQL escape hatch for
    agent code. If the deployed MCP server later grows a dedicated tracing
    tool, this function should be deleted, not generalised.
    """
    del mcp_client  # see docstring: kept for call-site clarity, not used here
    async with fde_db.tool_transaction() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)


@contextlib.contextmanager
def open_mcp_client(
    settings: GatewaySettings | None = None,
) -> Iterator[tuple[MCPClient, list[Any]]]:
    """Convenience wrapper: `with open_mcp_client() as (client, tools): ...`
    Returns both the started `MCPClient` and its `list_tools_sync()` result,
    since almost every call site needs both immediately.
    """
    client = build_mcp_client(settings)
    with client:
        tools = client.list_tools_sync()
        yield client, tools
