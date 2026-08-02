"""config.py -- environment configuration specific to the three AgentCore
agent runtimes (Engagement, Workflow, Development).

Design
------
`fde_mcp.config.get_settings()` already owns every `FDE_*` variable shared
with the MCP server -- database connection, embedding, and the base
`AgentSettings` (`runtime_arn`/`name`/`model_id`/`principal`/
`trace_session_id`) that both processes write into
`hitl.proposal.authored_by` / `trn.trace_session` identically (see that
module's docstring). Reimplementing any of that parsing here would give the
agent process and the MCP server two independent readings of the same env
var, which is exactly the kind of scatter `fde_mcp.config`'s own docstring
exists to prevent -- so every setting below either reads a variable
`fde_mcp.config` does not already know about, or wraps one of its values to
add an agent-specific default.

What lives here instead is everything an agent runtime needs that the MCP
server never does: how to reach the knowledge-graph tool surface (local
stdio subprocess vs. AgentCore Gateway -- see `mcp_tools.py`), how long an
invocation is willing to camp on a HITL gate before returning control (see
`hitl.py`), and the couple of knobs specific to one agent (the Development
agent's optional scaffold-output directory).

Same shape as `fde_mcp.config`: small frozen dataclasses, one `from_env()`
each, composed into one `AgentRuntimeSettings`, cached process-wide by
`get_agent_runtime_settings()` for the identical reason
`fde_mcp.config.get_settings()` is cached (see its docstring) -- these are
process configuration, not runtime state. Tests that monkeypatch
`os.environ` should call `get_agent_runtime_settings.cache_clear()` first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from fde_mcp.config import get_settings

# The model this platform's prompts have been tuned against. Overridable per
# deployment via `FDE_MODEL_ID` -- the SAME variable `fde_mcp.config.
# AgentSettings.model_id` reads for the audit trail written alongside every
# proposal, so the model id recorded on a proposal and the model id this
# process actually calls Bedrock with can never disagree. Only the
# *default* applied when the variable is unset is decided here, not there --
# "what happens when it's unset" is an agent-runtime concern, not something
# the MCP server (which never calls a model itself) needs an opinion on.
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# AgentCore's own ceilings this platform is built against (see hitl.py's
# module docstring): a 15-minute idle-ping reap (made irrelevant during a
# gate wait because `add_async_task` keeps ping `HEALTHY_BUSY` for its
# duration) and an 8-hour max session lifetime. This default is chosen to
# sit comfortably inside the second ceiling with a wide safety margin, not
# to approximate any gate's real SLA -- `db/004_hitl_gates.sql`'s default
# `sla_hours=72` will almost never resolve inside this window, and that is
# expected; see `hitl.await_human_gate`'s docstring.
DEFAULT_GATE_WAIT_TIMEOUT_S = float(20 * 60)


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_opt_str(name: str) -> str | None:
    return os.environ.get(name) or None


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """Everything `mcp_tools.build_mcp_client()` needs to pick a transport
    and connect. See that module's docstring for the dev-stdio/prod-Gateway
    split this backs.

    Attributes:
        url: `FDE_GATEWAY_URL`. Presence (not truthiness) selects Gateway
            mode -- an empty string is treated as unset so a blank env var
            injected by a container orchestrator does not silently flip an
            agent into a mode with no working URL.
        stdio_command: `FDE_MCP_SERVER_CMD`, default "python3". Dev-mode
            local stdio subprocess command.
        stdio_args: `FDE_MCP_SERVER_ARGS`, whitespace-split, default
            `("-m", "fde_mcp")`. That is the package's real entrypoint
            (`fde_mcp/__main__.py`); the previous `server.py` default was
            inherited from the pre-workspace flat layout and no longer
            exists on any path, so dev stdio mode could only ever fail.
        stdio_cwd: `FDE_MCP_SERVER_CWD`, default None -- inherit the agent
            process's own working directory. `python -m fde_mcp` resolves
            through the installed package, so pinning a directory is not
            only unnecessary, it is what made the old `/app/mcp` default
            wrong everywhere outside one container layout.
        identity_provider_name: `FDE_IDENTITY_PROVIDER_NAME`, default
            "fde-gateway-m2m". The AgentCore Identity M2M credential
            provider name used to mint the Gateway's bearer token.
        scopes: `FDE_GATEWAY_SCOPES`, comma-separated, default
            `("gateway:invoke",)`.
        startup_timeout_s: `FDE_MCP_STARTUP_TIMEOUT_S`, default 30.
        http_timeout_s: `FDE_MCP_HTTP_TIMEOUT_S`, default 45.0. Headroom
            above the MCP server's own 20s per-tool statement_timeout
            (`fde_mcp.config.DatabaseSettings.statement_timeout`) plus
            Gateway network overhead.
    """

    url: str | None
    stdio_command: str
    stdio_args: tuple[str, ...]
    stdio_cwd: str | None
    identity_provider_name: str
    scopes: tuple[str, ...]
    startup_timeout_s: int
    http_timeout_s: float

    @classmethod
    def from_env(cls) -> GatewaySettings:
        return cls(
            url=_env_opt_str("FDE_GATEWAY_URL"),
            stdio_command=_env_str("FDE_MCP_SERVER_CMD", "python3"),
            stdio_args=tuple(_env_str("FDE_MCP_SERVER_ARGS", "-m fde_mcp").split()),
            stdio_cwd=_env_opt_str("FDE_MCP_SERVER_CWD"),
            identity_provider_name=_env_str("FDE_IDENTITY_PROVIDER_NAME", "fde-gateway-m2m"),
            scopes=tuple(
                s for s in _env_str("FDE_GATEWAY_SCOPES", "gateway:invoke").split(",") if s
            ),
            startup_timeout_s=_env_int("FDE_MCP_STARTUP_TIMEOUT_S", 30),
            http_timeout_s=_env_float("FDE_MCP_HTTP_TIMEOUT_S", 45.0),
        )


@dataclass(frozen=True, slots=True)
class AgentProcessSettings:
    """Per-invocation runtime knobs for the agent's own entrypoint --
    everything `common/runtime.py` needs beyond what `fde_mcp.config`
    already provides.

    Attributes:
        model_id: `FDE_MODEL_ID`, via `fde_mcp.config`'s
            `AgentSettings.model_id`, defaulting to `DEFAULT_MODEL_ID` when
            unset (see that constant's comment for why the default lives
            here, not there).
        agent_qualifier: `FDE_AGENT_QUALIFIER`. AgentCore Runtime version
            qualifier, recorded on the trace session for audit.
        gate_wait_timeout_s: `FDE_GATE_WAIT_TIMEOUT_S`, default 1200 (20
            minutes) -- see `DEFAULT_GATE_WAIT_TIMEOUT_S`.
        scaffold_output_dir: `FDE_SCAFFOLD_OUTPUT_DIR`. Development agent
            only -- see `development/agent.py`'s `scaffold_agent` task. Left
            unset in every other deployment shape; writing to local disk
            only makes sense for a dev loop, since the normal AgentCore
            Runtime container filesystem is not a durable artifact store.
        runtime_arn_override: `FDE_AGENT_RUNTIME_ARN`, read directly here
            (rather than through `fde_mcp.config`'s already-defaulted
            value) because the two processes want different fallbacks when
            it is unset: the MCP server's own generic
            `"local-dev:fde-mcp-server"` placeholder would make three
            different agents' local trace rows indistinguishable by ARN
            alone. `resolved_runtime_arn()` supplies the agent-specific
            fallback instead.
    """

    model_id: str
    agent_qualifier: str | None
    gate_wait_timeout_s: float
    scaffold_output_dir: str | None
    runtime_arn_override: str | None

    @classmethod
    def from_env(cls) -> AgentProcessSettings:
        return cls(
            model_id=get_settings().agent.model_id or DEFAULT_MODEL_ID,
            agent_qualifier=_env_opt_str("FDE_AGENT_QUALIFIER"),
            gate_wait_timeout_s=_env_float("FDE_GATE_WAIT_TIMEOUT_S", DEFAULT_GATE_WAIT_TIMEOUT_S),
            scaffold_output_dir=_env_opt_str("FDE_SCAFFOLD_OUTPUT_DIR"),
            runtime_arn_override=_env_opt_str("FDE_AGENT_RUNTIME_ARN"),
        )

    def resolved_runtime_arn(self, agent_key: str) -> str:
        """The ARN recorded as `trn.trace_session.agent_runtime_arn` /
        `hitl.proposal.authored_by` for this process.

        `agent_key` is one of `"engagement"`/`"workflow"`/`"development"`,
        supplied by each agent's own `common.runtime.AgentRuntimeConfig`
        (a compile-time identity, not something read from the
        environment -- each of the three agents is its own process/module,
        never a single process deciding at runtime which persona to be).
        """
        return self.runtime_arn_override or f"local-dev:{agent_key}-agent"


@dataclass(frozen=True, slots=True)
class AgentRuntimeSettings:
    """The whole agent-runtime-specific process configuration, assembled
    once by `from_env()` and handed out by `get_agent_runtime_settings()`.
    """

    gateway: GatewaySettings
    process: AgentProcessSettings

    @classmethod
    def from_env(cls) -> AgentRuntimeSettings:
        return cls(gateway=GatewaySettings.from_env(), process=AgentProcessSettings.from_env())


@lru_cache(maxsize=1)
def get_agent_runtime_settings() -> AgentRuntimeSettings:
    """Return the process-wide agent runtime settings, reading `os.environ`
    on first call and caching thereafter -- see `fde_mcp.config.
    get_settings`'s docstring for why caching (not a module-level constant,
    not a fresh read every call) is the right shape here too. Tests that
    monkeypatch `os.environ` should call
    `get_agent_runtime_settings.cache_clear()` first.
    """
    return AgentRuntimeSettings.from_env()
