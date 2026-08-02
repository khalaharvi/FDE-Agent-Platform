"""config.py -- every environment variable this package reads, declared once.

Shape copied deliberately from `fde_mcp.config` and `fde_training.config`:
a frozen dataclass built by `from_env()` and handed out by an `lru_cache`d
`get_gate_settings()`. Frozen because a request reading
`get_gate_settings().prodops_role` halfway through a run advance must never
observe a value that changed under it; `lru_cache`d rather than a
module-level constant so a test can `get_gate_settings.cache_clear()` after
monkeypatching `os.environ` and observe a fresh read.

Database settings are NOT redeclared here
------------------------------------------
This service talks to the same Postgres, addressed by the same `FDE_DB_*`
variables, as the MCP server -- so `fde_mcp.config.DatabaseSettings` is
reused rather than copied, exactly as `fde_training.config` does. What is
new here is only the two ROLES this process downgrades to (`fde_gate_service`
and `fde_prodops`), which `fde_mcp` never runs as, and the handful of
Lambda/AgentCore/MCP endpoint knobs the runner needs.

`FDE_DB_ROLE` is deliberately not consulted anywhere in this package. That
variable means "the role the MCP server's tool calls run as" (`fde_agent`),
and a gate service that silently inherited it would run reviewer merges as
the agent role -- which would fail, loudly, but only at the moment a human
clicked approve.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from fde_mcp.config import DatabaseSettings
from fde_mcp.config import get_settings as _get_mcp_settings

# Which AgentCore runtime an `agent` step targets is chosen by the step's
# own `tool_name`, which must be one of these three persona names. Kept as a
# tuple next to the settings that hold the ARNs so the two cannot drift.
AGENT_RUNTIME_NAMES: tuple[str, ...] = ("engagement", "workflow", "development")


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_opt_str(name: str) -> str | None:
    return os.environ.get(name)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True, slots=True)
class GateSettings:
    """Process configuration for the gate service Lambda.

    Attributes:
        gate_role: `FDE_GATE_DB_ROLE`, default "fde_gate_service". The role
            every proposal/review/merge/publish/label/expiry transaction
            downgrades to. It is the only role in the platform holding
            EXECUTE on `hitl.merge_proposal`, `hitl.apply_trace_label` and
            `wf.publish_workflow` (db/010, db/013).
        prodops_role: `FDE_PRODOPS_DB_ROLE`, default "fde_prodops". The role
            every workflow-run and drift-triage transaction downgrades to.
            Deliberately narrower than `gate_role`: product ops runs
            workflows, and publishing one is a gated action it must not be
            able to perform (db/010:64).
        self_function_name: `FDE_GATE_FUNCTION_NAME`. This Lambda's own
            function name. When set, an `advance` that reaches an `agent` or
            `tool` step re-invokes this function asynchronously and returns
            202 rather than risking API Gateway's 29-second cap; the async
            invocation gets the full Lambda timeout. When unset -- which is
            the case under pytest and in any local run -- the step executes
            inline instead. Losing an async invoke is survivable: the
            one-minute tick re-advances any run left with no open step.
        runtime_arn_engagement: `FDE_RUNTIME_ARN_ENGAGEMENT`. AgentCore
            runtime ARN invoked by an `agent` step whose `tool_name` is
            "engagement". Unset means such a step fails honestly through
            `on_failure` rather than appearing to succeed.
        runtime_arn_workflow: `FDE_RUNTIME_ARN_WORKFLOW`. As above, for
            "workflow".
        runtime_arn_development: `FDE_RUNTIME_ARN_DEVELOPMENT`. As above,
            for "development".
        mcp_url: `FDE_MCP_URL`. The streamable-HTTP MCP endpoint a `tool`
            step calls (the AgentCore Gateway URL, or the MCP server
            directly). Unset means `tool` steps fail through `on_failure`.
        mcp_token: `FDE_MCP_TOKEN`. Bearer token sent to `mcp_url`
            (a Cognito M2M client-credentials access token in a deployed
            environment). Optional -- an endpoint behind IAM/VPC auth needs
            no bearer.
        max_step_attempts: `FDE_GATE_MAX_STEP_ATTEMPTS`, default 3. Passed
            to `wf.fail_step` as `p_max_attempts`; a step whose `on_failure`
            is 'retry' fails the whole run once its attempt count reaches
            this. The retry POLICY lives in SQL -- this is only the number.
        request_timeout_seconds: `FDE_GATE_STEP_TIMEOUT_SECONDS`, default
            870. Client-side ceiling on one agent/tool step execution,
            chosen to sit just inside Lambda's 900-second maximum so an
            overrunning step returns a `StepExecutionError` (routed through
            `on_failure`, leaving a readable error on the attempt) instead
            of the Lambda being killed mid-step and the row being swept up
            later by `wf.timeout_steps`.
        aws_region: `AWS_REGION`, falling back to `AWS_DEFAULT_REGION`. Not
            `FDE_`-prefixed because it is the standard AWS SDK variable and
            inventing a second one only creates a place for the two to
            disagree -- the same call `fde_mcp.config` makes.
    """

    gate_role: str
    prodops_role: str
    self_function_name: str | None
    runtime_arn_engagement: str | None
    runtime_arn_workflow: str | None
    runtime_arn_development: str | None
    mcp_url: str | None
    mcp_token: str | None
    max_step_attempts: int
    request_timeout_seconds: int
    aws_region: str | None

    @classmethod
    def from_env(cls) -> GateSettings:
        return cls(
            gate_role=_env_str("FDE_GATE_DB_ROLE", "fde_gate_service"),
            prodops_role=_env_str("FDE_PRODOPS_DB_ROLE", "fde_prodops"),
            self_function_name=_env_opt_str("FDE_GATE_FUNCTION_NAME"),
            runtime_arn_engagement=_env_opt_str("FDE_RUNTIME_ARN_ENGAGEMENT"),
            runtime_arn_workflow=_env_opt_str("FDE_RUNTIME_ARN_WORKFLOW"),
            runtime_arn_development=_env_opt_str("FDE_RUNTIME_ARN_DEVELOPMENT"),
            mcp_url=_env_opt_str("FDE_MCP_URL"),
            mcp_token=_env_opt_str("FDE_MCP_TOKEN"),
            max_step_attempts=_env_int("FDE_GATE_MAX_STEP_ATTEMPTS", 3),
            request_timeout_seconds=_env_int("FDE_GATE_STEP_TIMEOUT_SECONDS", 870),
            aws_region=_env_opt_str("AWS_REGION") or _env_opt_str("AWS_DEFAULT_REGION"),
        )

    def runtime_arn_for(self, agent_name: str | None) -> str | None:
        """The AgentCore runtime ARN for one of the three agent personas.

        Returns None for an unknown name or an unconfigured ARN; the agent
        executor turns that into a `StepExecutionError` naming the variable
        to set, rather than invoking some other agent.
        """
        return {
            "engagement": self.runtime_arn_engagement,
            "workflow": self.runtime_arn_workflow,
            "development": self.runtime_arn_development,
        }.get(agent_name or "")


@dataclass(frozen=True, slots=True)
class Settings:
    """The whole process configuration.

    Attributes:
        db: See `fde_mcp.config.DatabaseSettings` -- reused, not copied; see
            this module's docstring.
        gate: See `GateSettings`.
    """

    db: DatabaseSettings
    gate: GateSettings

    @classmethod
    def from_env(cls) -> Settings:
        return cls(db=_get_mcp_settings().db, gate=GateSettings.from_env())


@lru_cache(maxsize=1)
def get_gate_settings() -> Settings:
    """Return the process-wide `Settings`, reading `os.environ` on first call.

    Tests that monkeypatch the environment must call
    `get_gate_settings.cache_clear()` -- and, because `Settings.db` is
    sourced from `fde_mcp.config`, that module's `get_settings.cache_clear()`
    as well.
    """
    return Settings.from_env()
