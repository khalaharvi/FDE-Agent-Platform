"""Trace-session and trace-step recording for the training substrate.

Design intent
-------------
`db/009_training.sql`'s entire training strategy rests on being able to
reconstruct *exactly* what an agent saw and said, turn by turn, joined to the
human decision that eventually labelled it. Two invariants make that
possible, and this module exists to guarantee both of them from the agent
side (`fde_mcp.tools._base.emit_trace` handles the *tool*-turn half of the
picture -- see that module's docstring -- this module handles the session
row and the user/assistant turns the MCP server never sees):

1. **One `trn.trace_session` row per AgentCore session, created before the
   first turn, with `base_commit_id` pinned from `kg_head_commit` at start.**
   Without the pin, a trace is untrainable -- you cannot reconstruct what
   graph state the model could have seen (see 009's module docstring). We
   pin it once, here, at the moment the agent's `runtimeSessionId` becomes
   known, rather than trusting each downstream tool call to have done it.

2. **`trainable` is set correctly per role, with no exceptions.** Every
   assistant turn is `trainable=true`; every tool and user turn is
   `trainable=false`. This is the single column the SFT collator and the RL
   rollout both key their loss-masking off (`db/009_training.sql`, `trn.
   trace_step` column comment). Getting this backwards for even one message
   type silently trains the policy to hallucinate tool output or to imitate
   user prompts, which is exactly the failure mode 009's docstring warns
   about -- so `record_turn` below hard-codes the mapping rather than taking
   a boolean the caller could get wrong.

`fde_mcp.server`'s tool surface has no `trace_session_start`/`trace_step_
record` tool -- tracing is something that server does FOR ITSELF on every
tool call (`emit_trace`), but nothing in it writes the user/assistant turns
an agent orchestrator produces before/between tool calls. Rather than add a
new MCP tool whose only job is an INSERT into a table `fde_agent` can
already write to (which would mean a full Gateway round trip per
conversational turn, purely for telemetry), this module writes `trn.
trace_session` / `trn.trace_step` directly, via `mcp_tools.raw_sql`, using
the *same* connection contract (`SET LOCAL ROLE fde_agent`, per-call
statement_timeout) documented in `fde_mcp.db` -- see that module's docstring
for exactly why this is the one sanctioned exception to "agents only reach
state through MCP tools." This keeps the "which principal wrote this row"
story identical to the MCP server's own tool-call tracing, without a new
network hop through the Gateway for every single turn.

OTEL
----
Every session and turn also emits an OpenTelemetry span
(`fde.agent.session` / `fde.agent.turn`) when the `opentelemetry` API is
importable, so CloudWatch GenAI Observability (which AgentCore wires up
automatically for spans tagged with the runtime's ambient resource
attributes) shows the same trajectory a human reviewer would see in
`trn.trace_step`. If `opentelemetry` is not installed, every span operation
becomes a no-op context manager -- this module must never fail an agent turn
because a tracing dependency is missing.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

import psycopg.errors as pg_errors

from fde_mcp.logging import get_logger

from . import mcp_tools

log = get_logger(__name__)

try:  # pragma: no cover - exercised only when opentelemetry is absent
    from opentelemetry import trace as _otel_trace

    _TRACER: Any = _otel_trace.get_tracer("fde.agents")
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - degrade gracefully, tracing is best-effort
    _TRACER = None
    _OTEL_AVAILABLE = False


@contextlib.contextmanager
def _otel_span(name: str, attributes: dict[str, Any] | None = None) -> Generator[Any, None, None]:
    """No-op-safe OTEL span. Never raises, regardless of OTEL availability
    or of what happens inside the `with` block's caller-visible exceptions
    (those still propagate -- only span *creation* is defensive).
    """
    if not _OTEL_AVAILABLE:
        yield None
        return
    try:
        with _TRACER.start_as_current_span(name) as span:
            for key, value in (attributes or {}).items():
                with contextlib.suppress(Exception):  # a bad attribute must not break tracing
                    span.set_attribute(key, value)
            yield span
    except Exception:  # a broken OTEL SDK must not break tracing
        log.debug("otel_span_start_failed", span_name=name, exc_info=True)
        yield None


# Loss-masking contract from db/009_training.sql: assistant turns train,
# everything else does not. Hard-coded on purpose -- see module docstring.
_TRAINABLE_BY_ROLE = {
    "system": False,
    "user": False,
    "assistant": True,
    "tool": False,
}


@dataclass
class TraceSession:
    """In-process handle for one AgentCore session's trace.

    `session_id` MUST equal the AgentCore `runtimeSessionId` the payload
    arrived with -- that identity is what lets CloudWatch spans and
    `trn.trace_step` rows join on a single key (see 009's module docstring).
    """

    session_id: str
    engagement_id: str
    agent_name: str
    agent_runtime_arn: str
    model_id: str
    base_commit_id: int
    task_kind: str
    task_input: dict[str, Any]
    agent_qualifier: str | None = None
    policy_version: str | None = None
    _turn: int = field(default=0, repr=False)
    _span_cm: Any = field(default=None, repr=False)
    _span: Any = field(default=None, repr=False)

    def next_turn(self) -> int:
        self._turn += 1
        return self._turn


async def start_session(
    mcp_client: Any,
    *,
    session_id: str,
    engagement_id: str,
    agent_name: str,
    agent_runtime_arn: str,
    model_id: str,
    task_kind: str,
    task_input: dict[str, Any],
    agent_qualifier: str | None = None,
    policy_version: str | None = None,
) -> TraceSession:
    """Open a `trn.trace_session` row, pinning `base_commit_id` from
    `kg_head_commit` right now. Must be called before the first turn of a
    task so every subsequent trace_step (and every proposal the task raises)
    can cite a `base_commit_id` that is reproducible after the fact.

    Raises if `kg_head_commit` reports `has_head=False` -- an agent cannot
    reason over a graph that has never been merged into, and a trace with a
    null commit pin is unrecoverable for training (see module docstring).
    """
    head = await mcp_client.call_tool_async(
        tool_use_id=f"trace-start-{uuid.uuid4().hex[:8]}",
        name="kg_head_commit",
        arguments={"engagement_id": engagement_id},
    )
    head_payload = _extract_tool_json(head)
    if not head_payload or not head_payload.get("has_head"):
        msg = (
            f"engagement {engagement_id} has no sealed HEAD commit; cannot "
            "pin base_commit_id for a trainable trace session"
        )
        raise RuntimeError(msg)
    base_commit_id = int(head_payload["commit_id"])

    session = TraceSession(
        session_id=session_id,
        engagement_id=engagement_id,
        agent_name=agent_name,
        agent_runtime_arn=agent_runtime_arn,
        model_id=model_id,
        base_commit_id=base_commit_id,
        task_kind=task_kind,
        task_input=task_input,
        agent_qualifier=agent_qualifier,
        policy_version=policy_version,
    )

    span_cm = _otel_span(
        "fde.agent.session",
        {
            "fde.session_id": session_id,
            "fde.engagement_id": engagement_id,
            "fde.agent_name": agent_name,
            "fde.task_kind": task_kind,
            "fde.base_commit_id": base_commit_id,
        },
    )
    session._span_cm = span_cm
    session._span = span_cm.__enter__()

    await _insert_trace_session(mcp_client, session)
    log.info(
        "trace_session_started",
        agent_name=agent_name,
        task_kind=task_kind,
        base_commit_id=base_commit_id,
    )
    return session


async def end_session(
    mcp_client: Any,
    session: TraceSession,
    *,
    outcome: str = "pending",
    final_output: dict[str, Any] | None = None,
    total_tokens: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """Attempt to close out the session row (`ended_at`, `outcome`,
    `final_output`, `total_tokens`, `latency_ms`).

    `fde_agent` -- the role every write in this module runs as (see
    `mcp_tools.raw_sql`'s docstring) -- has `INSERT, SELECT` on
    `trn.trace_session` plus, per `db/011_mcp_agent_supplemental_grants.
    sql`, `UPDATE` on exactly four self-reported columns: `ended_at`,
    `final_output`, `total_tokens`, `latency_ms`. The columns this function
    also *tries* to set in its `UPDATE` statement -- `outcome` -- is
    deliberately NOT among them: `outcome`/`label_proposal_id`/
    `label_source`/`split` are training LABELS, reserved for the gate
    service and `fde_training` to set from the actual HITL gate resolution,
    never from the agent's own self-report (see `db/009_training.sql`'s
    docstring for why a merged/rejected/edited proposal is the trustworthy
    label in the first place -- an agent that could label its own work
    could mark its own output "accepted").

    Because that grant is column-scoped, not table-scoped, Postgres accepts
    the UPDATE as a whole and rejects only the disallowed column reference,
    so the `SET outcome = ...` in `_update_trace_session`'s statement fails
    with `InsufficientPrivilege` while the other four columns would have
    succeeded had they been issued alone. `_execute_sql` below treats that
    specific, expected failure as informational (logged once at INFO, not a
    warning-with-stack-trace) rather than as an anomaly to alert on -- this
    is a known, structural policy boundary, not a fault. Any OTHER exception
    (a real connectivity problem, a malformed session id) is still logged as
    a warning by `_execute_sql`, unchanged.
    """
    await _update_trace_session(
        mcp_client,
        session,
        outcome=outcome,
        final_output=final_output,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
    )
    if session._span_cm is not None:
        with contextlib.suppress(Exception):
            session._span_cm.__exit__(None, None, None)
    log.info("trace_session_ended", outcome=outcome)


async def record_turn(
    mcp_client: Any,
    session: TraceSession,
    *,
    role: str,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    tool_result: dict[str, Any] | None = None,
    retrieval: dict[str, Any] | None = None,
    latency_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> int:
    """Append one `trn.trace_step` row for a system/user/assistant turn.

    Deliberately does NOT accept a `trainable` argument -- see module
    docstring. `role` alone determines it via `_TRAINABLE_BY_ROLE`, and an
    unrecognised role is a programming error, not a value to guess about.

    Tool turns that the MCP server already logged via its own `emit_trace`
    (see `fde_mcp.tools._base`) should NOT be re-recorded here -- this
    function is for turns the MCP server never sees: the system prompt,
    user/task input, and the assistant's own messages (including the
    tool_calls it emitted, OpenAI-message-shape, per 009's `trn.trace_step`
    column comments). If an agent implementation drives tool calls itself
    rather than through the MCP server's tracing hook (e.g. a Strands
    callback intercepts the raw tool result before/instead of the MCP
    roundtrip), pass role='tool' here with trainable forced false
    regardless -- the mapping still applies.
    """
    if role not in _TRAINABLE_BY_ROLE:
        msg = f"unknown trace role {role!r}; must be one of {sorted(_TRAINABLE_BY_ROLE)}"
        raise ValueError(msg)
    trainable = _TRAINABLE_BY_ROLE[role]
    turn = session.next_turn()

    with _otel_span(
        "fde.agent.turn",
        {
            "fde.session_id": session.session_id,
            "fde.turn": turn,
            "fde.role": role,
            "fde.trainable": trainable,
            **({"fde.tool_name": tool_name} if tool_name else {}),
        },
    ):
        await _insert_trace_step(
            mcp_client,
            session,
            turn=turn,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_result=tool_result,
            trainable=trainable,
            retrieval=retrieval,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    return turn


# ---------------------------------------------------------------------------
# Low-level persistence. Isolated behind these two functions so a future
# swap to a dedicated `trace_step_record` MCP tool (if the server grows one)
# only touches this module.
# ---------------------------------------------------------------------------
async def _insert_trace_session(mcp_client: Any, session: TraceSession) -> None:
    sql = """
        INSERT INTO trn.trace_session
            (session_id, engagement_id, agent_name, agent_runtime_arn,
             agent_qualifier, model_id, policy_version, base_commit_id,
             task_kind, task_input, outcome, started_at)
        VALUES (%(session_id)s::uuid, %(engagement_id)s::uuid, %(agent_name)s,
                %(agent_runtime_arn)s, %(agent_qualifier)s, %(model_id)s,
                %(policy_version)s, %(base_commit_id)s, %(task_kind)s,
                %(task_input)s::jsonb, 'pending', now())
        ON CONFLICT (session_id) DO NOTHING
    """
    params = {
        "session_id": session.session_id,
        "engagement_id": session.engagement_id,
        "agent_name": session.agent_name,
        "agent_runtime_arn": session.agent_runtime_arn,
        "agent_qualifier": session.agent_qualifier,
        "model_id": session.model_id,
        "policy_version": session.policy_version,
        "base_commit_id": session.base_commit_id,
        "task_kind": session.task_kind,
        "task_input": json.dumps(session.task_input, default=str),
    }
    await _execute_sql(mcp_client, sql, params)


async def _update_trace_session(
    mcp_client: Any,
    session: TraceSession,
    *,
    outcome: str,
    final_output: dict[str, Any] | None,
    total_tokens: int | None,
    latency_ms: int | None,
) -> None:
    sql = """
        UPDATE trn.trace_session
           SET outcome = %(outcome)s::trn.trace_outcome,
               final_output = COALESCE(%(final_output)s::jsonb, final_output),
               total_tokens = COALESCE(%(total_tokens)s, total_tokens),
               latency_ms = COALESCE(%(latency_ms)s, latency_ms),
               ended_at = now()
         WHERE session_id = %(session_id)s::uuid
    """
    params = {
        "session_id": session.session_id,
        "outcome": outcome,
        "final_output": json.dumps(final_output, default=str) if final_output is not None else None,
        "total_tokens": total_tokens,
        "latency_ms": latency_ms,
    }
    await _execute_sql(mcp_client, sql, params)


async def _insert_trace_step(
    mcp_client: Any,
    session: TraceSession,
    *,
    turn: int,
    role: str,
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
    tool_call_id: str | None,
    tool_name: str | None,
    tool_result: dict[str, Any] | None,
    trainable: bool,
    retrieval: dict[str, Any] | None,
    latency_ms: int | None,
    tokens_in: int | None,
    tokens_out: int | None,
) -> None:
    sql = """
        INSERT INTO trn.trace_step
            (session_id, turn, role, content, tool_calls, tool_call_id,
             tool_name, tool_result, trainable, retrieval, latency_ms,
             tokens_in, tokens_out)
        VALUES (%(session_id)s::uuid, %(turn)s, %(role)s, %(content)s,
                %(tool_calls)s::jsonb, %(tool_call_id)s, %(tool_name)s,
                %(tool_result)s::jsonb, %(trainable)s, %(retrieval)s::jsonb,
                %(latency_ms)s, %(tokens_in)s, %(tokens_out)s)
        ON CONFLICT (session_id, turn) DO NOTHING
    """
    params = {
        "session_id": session.session_id,
        "turn": turn,
        "role": role,
        "content": content,
        "tool_calls": json.dumps(tool_calls, default=str) if tool_calls is not None else None,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "tool_result": json.dumps(tool_result, default=str) if tool_result is not None else None,
        "trainable": trainable,
        "retrieval": json.dumps(retrieval, default=str) if retrieval is not None else None,
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }
    await _execute_sql(mcp_client, sql, params)


async def _execute_sql(mcp_client: Any, sql: str, params: dict[str, Any]) -> None:
    """Best-effort direct write against the trace tables.

    Delegates to `mcp_tools.raw_sql`, which goes through `fde_mcp.db.
    tool_transaction()`'s exact contract (SET LOCAL ROLE fde_agent, 20s
    statement_timeout) -- see that function's docstring for why this is the
    one sanctioned exception to "agents only reach state through MCP
    tools." Any failure here is logged and swallowed: a tracing gap must
    never fail the agent's actual task, exactly like the MCP server's own
    `emit_trace` (see `fde_mcp.tools._base`).
    """
    try:
        await mcp_tools.raw_sql(mcp_client, sql, params)
    except Exception as exc:
        if _is_expected_trace_grant_gap(exc):
            # See end_session's docstring: db/011 grants fde_agent UPDATE on
            # exactly (ended_at, final_output, total_tokens, latency_ms) on
            # trn.trace_session. Reaching here on an UPDATE means the SET
            # outcome = ... clause in the same statement was rejected --
            # outcome/label_proposal_id/label_source/split are training
            # labels, reserved for the gate service and fde_training, never
            # for an agent's own self-report. Log quietly; this is a policy
            # boundary, not a fault.
            log.info(
                "trace_update_partially_rejected",
                reason=(
                    "fde_agent may write only (ended_at, final_output, "
                    "total_tokens, latency_ms) on trn.trace_session per "
                    "db/011; label columns are withheld by design"
                ),
            )
            return
        log.warning(
            "trace_write_failed", sql_preview=sql.strip().splitlines()[1][:80], exc_info=True
        )


def _is_expected_trace_grant_gap(exc: Exception) -> bool:
    return isinstance(exc, pg_errors.InsufficientPrivilege)


def _extract_tool_json(tool_result: Any) -> dict[str, Any] | None:
    """Strands MCP tool results come back as a `ToolResult` dict with a
    `content` list of blocks; the JSON payload is usually the last block's
    `json` field (structured content) or its `text` field (stringified
    JSON). Isolated here because both `tracing.py` and `hitl.py` need to
    unwrap the same shape.
    """
    if tool_result is None:
        return None
    if isinstance(tool_result, dict) and "commit_id" in tool_result:
        return tool_result  # already unwrapped
    content = tool_result.get("content") if isinstance(tool_result, dict) else None
    if not content:
        return None
    last = content[-1]
    if isinstance(last, dict):
        if "json" in last and isinstance(last["json"], dict):
            return last["json"]
        text = last.get("text")
        if isinstance(text, str):
            with contextlib.suppress(json.JSONDecodeError):
                result: dict[str, Any] = json.loads(text)
                return result
    return None
