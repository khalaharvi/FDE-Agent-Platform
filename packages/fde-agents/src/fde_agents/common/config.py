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

# The default model. Overridable per deployment via `FDE_MODEL_ID` -- the
# SAME variable `fde_mcp.config.AgentSettings.model_id` reads for the audit
# trail written alongside every proposal, so the model id recorded on a
# proposal and the model id this process actually calls Bedrock with can
# never disagree. Only the *default* applied when the variable is unset is
# decided here, not there -- "what happens when it's unset" is an
# agent-runtime concern, not something the MCP server (which never calls a
# model itself) needs an opinion on.
#
# Claude Sonnet 5 (Bedrock id: current-generation models are date-less and
# `anthropic.`-prefixed). Two migration notes from Sonnet 4.5, which the
# original prompts were authored against: the new tokenizer produces ~30%
# more tokens for the same text (per-token price unchanged -- re-baseline
# token budgets, not the rates), and adaptive thinking is on by default.
# Verify availability in your region with `aws bedrock list-foundation-models`
# before deploying (README "Status and honesty").
DEFAULT_MODEL_ID = "anthropic.claude-sonnet-5"

# ---------------------------------------------------------------------
# Model presets -- `FDE_MODEL_PRESET`, the budget dial.
#
# Resolution order for the model an agent process actually calls:
#   explicit `FDE_MODEL_ID`  >  the preset's entry for this agent  >
#   `DEFAULT_MODEL_ID`. Decide-for-me with an escape hatch, in that order.
# The deploy CLI (`fde-agents-deploy runtimes --model-preset ...`) resolves
# a preset to a concrete per-runtime `FDE_MODEL_ID` at provisioning time,
# which keeps the audit-trail property above intact: the model id recorded
# on a proposal is always the model id that authored it.
#
# Why these tiers (cost math in README "Choosing models"): every write an
# agent produces still passes the same human gates and fail-closed
# validation, which makes this platform unusually tolerant of cheaper
# models -- sloppiness lands in a review queue, not in the graph. The
# shipped prompts were tuned against Claude; the open-weight rows follow
# Bedrock's published `zai.*` naming but have NOT been exercised from this
# repo (README "Status and honesty") -- verify with
# `aws bedrock list-foundation-models` before budgeting around them. A
# cheaper JUDGE for the rival grader is a separate decision with its own
# measured gate: `fde-training rival-grader calibrate` must show
# kappa >= 0.78 first (docs/06 section 6).
# ---------------------------------------------------------------------
MODEL_PRESETS: dict[str, dict[str, str]] = {
    # All-Claude. What the shipped prompts were tuned against.
    "premium": {
        "engagement": DEFAULT_MODEL_ID,
        "workflow": DEFAULT_MODEL_ID,
        "development": DEFAULT_MODEL_ID,
    },
    # Tiered-conservative: agentic open-weight model on the two
    # propose-and-gate agents; Claude stays on codegen (scaffolds ship as
    # reviewable packages, but generated code quality is worth the spend).
    "balanced": {
        "engagement": "zai.glm-5",
        "workflow": "zai.glm-5",
        "development": DEFAULT_MODEL_ID,
    },
    # Tiered-aggressive: open-weight everywhere. Cheapest defensible mix --
    # small models stay OUT of the 21-tool loop entirely; this dial selects
    # among capable agentic models, it never degrades below them.
    "budget": {
        "engagement": "zai.glm-5",
        "workflow": "zai.glm-5",
        "development": "zai.glm-5",
    },
}


def resolve_model_id(agent_key: str) -> str:
    """The model this agent process should call, per the resolution order
    documented on `MODEL_PRESETS`. Fails loudly on an unknown preset name
    or an agent missing from a preset -- a typo here silently selects a
    differently-priced model otherwise, which is exactly the failure a
    budget dial must not have.
    """
    explicit = get_settings().agent.model_id
    if explicit:
        return explicit
    provider = ModelBackendSettings.from_env().provider  # cheap; no cache interplay
    if provider != "bedrock":
        # `explicit` is already known falsy here -- the unconditional check
        # above returned already if it were set. mypy --strict flags a
        # second `if explicit: return explicit` here as unreachable.
        msg = (
            f"FDE_MODEL_PROVIDER={provider!r} requires an explicit FDE_MODEL_ID "
            "(MODEL_PRESETS name Bedrock model ids, which mean nothing to other "
            "providers)"
        )
        raise ValueError(msg)
    preset_name = os.environ.get("FDE_MODEL_PRESET")
    if not preset_name:
        return DEFAULT_MODEL_ID
    preset = MODEL_PRESETS.get(preset_name)
    if preset is None:
        msg = (
            f"FDE_MODEL_PRESET={preset_name!r} is not a preset; choose one of "
            f"{sorted(MODEL_PRESETS)} or set FDE_MODEL_ID explicitly"
        )
        raise ValueError(msg)
    model_id = preset.get(agent_key)
    if model_id is None:
        msg = (
            f"model preset {preset_name!r} has no entry for agent {agent_key!r}; "
            f"known agents: {sorted(preset)}"
        )
        raise ValueError(msg)
    return model_id


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
class ModelBackendSettings:
    """Which inference backend authors agent turns, and how to reach it.

    Attributes:
        provider: `FDE_MODEL_PROVIDER`, default "bedrock". One of
            providers.PROVIDER_KEYS; validated in providers.build_model
            (not here) so the error can name every valid value.
        base_url: `FDE_MODEL_BASE_URL`. openai-compat endpoint; presence
            wins over `compat_preset`.
        compat_preset: `FDE_MODEL_COMPAT_PRESET`. Named openai-compat
            endpoint (see providers.COMPAT_PRESETS).
        max_tokens: `FDE_MODEL_MAX_TOKENS`, default 8192. Anthropic's
            Messages API requires an explicit ceiling; other providers
            use their own defaults and ignore this.
    """

    provider: str
    base_url: str | None
    compat_preset: str | None
    max_tokens: int

    @classmethod
    def from_env(cls) -> ModelBackendSettings:
        return cls(
            provider=_env_str("FDE_MODEL_PROVIDER", "bedrock"),
            base_url=_env_opt_str("FDE_MODEL_BASE_URL"),
            compat_preset=_env_opt_str("FDE_MODEL_COMPAT_PRESET"),
            max_tokens=_env_int("FDE_MODEL_MAX_TOKENS", 8192),
        )


@dataclass(frozen=True, slots=True)
class AgentProcessSettings:
    """Per-invocation runtime knobs for the agent's own entrypoint --
    everything `common/runtime.py` needs beyond what `fde_mcp.config`
    already provides.

    The model id is NOT a field here: it is per-agent (presets map each
    agent to its own model), so `common/runtime.py` resolves it at turn
    start via `resolve_model_id(agent_key)` -- one call feeding both the
    trace session's audit column and the `BedrockModel` actually invoked,
    so the two can never disagree.

    Attributes:
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

    agent_qualifier: str | None
    gate_wait_timeout_s: float
    scaffold_output_dir: str | None
    runtime_arn_override: str | None

    @classmethod
    def from_env(cls) -> AgentProcessSettings:
        return cls(
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
    model_backend: ModelBackendSettings

    @classmethod
    def from_env(cls) -> AgentRuntimeSettings:
        return cls(
            gateway=GatewaySettings.from_env(),
            process=AgentProcessSettings.from_env(),
            model_backend=ModelBackendSettings.from_env(),
        )


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
