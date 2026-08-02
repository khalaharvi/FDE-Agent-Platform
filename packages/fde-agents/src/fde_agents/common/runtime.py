"""The shared `BedrockAgentCoreApp` scaffolding all three agents build on.

Why this module exists
-----------------------
Before this module existed, `engagement/agent.py`, `workflow/agent.py`, and
`development/agent.py` each hand-rolled the same ~150 lines: constructing
the `BedrockAgentCoreApp`, an `/ping` handler mirroring the runtime's own
`HEALTHY`/`HEALTHY_BUSY` logic, task/engagement_id validation, binding
`FDE_TRACE_SESSION_ID` into the environment for the local-dev MCP
subprocess, opening the MCP client and starting a trace session, streaming
`streaming.run_agent_turn` and forwarding its events, running the
`guardrails.check_turn` pass over the final text, computing the session
`outcome`, camping on any resulting HITL gates via `hitl.await_human_gate`,
and closing out the trace session. None of that logic is specific to any
one agent's *purpose* -- what differs between the three is a system prompt,
a set of valid task names, how a task maps to a concrete prompt, which MCP
tools an agent may see, and a handful of narrow hooks (the Workflow agent's
post-hoc `wf_draft` faithfulness check, the Development agent's
`workflow_id` precondition and scaffold-file extraction).

Three copies of the shared 150 lines is exactly the failure mode this
platform's own docstrings warn about elsewhere (see `streaming.py`'s
module docstring on why its loop is factored once): a fix to, say, the
outcome-computation rule, or to how a gate-wait progress event is framed,
applied to two of the three files and not the third would silently produce
divergent SSE contracts and divergent `trn.trace_session.outcome` semantics
per agent -- exactly the kind of drift that corrupts the SFT export
(`trn.sft_export`) without ever raising an exception anywhere.

`AgentRuntimeConfig` is the seam: each agent module builds one (system
prompt, task table, a handful of small pure functions) and passes it to
`create_app()`, which returns a ready-to-run `BedrockAgentCoreApp` plus the
bare `handler` coroutine (kept separate and directly importable, per the
original design, so entrypoint logic is unit-testable without a live
Starlette app).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from bedrock_agentcore.runtime import BedrockAgentCoreApp, PingStatus
from strands import Agent
from strands.models.bedrock import BedrockModel

from fde_mcp.logging import bind_session, get_logger

from . import guardrails, hitl, mcp_tools, streaming, tracing
from .config import get_agent_runtime_settings, resolve_model_id
from .guardrails import Violation

log = get_logger(__name__)

TaskPayload = dict[str, Any]
StreamEvent = dict[str, Any]

# Hooks each agent supplies. Kept as plain, synchronous, side-effect-free
# functions of primitive/dict arguments (never `async def`, never given the
# live `mcp_client`/`app`) so an agent module's own tests can call them
# directly with plain dicts -- none of the three agents' current hooks need
# to await anything, and keeping that true is itself a useful constraint:
# a hook that "needs" to make a network call is a sign the behaviour
# belongs in `run_agent_task` itself, reachable by every agent, not hidden
# in one agent's private hook.
PromptBuilder = Callable[[str, str, str, TaskPayload], str]
ToolFilter = Callable[[list[Any]], list[Any]]
InputValidator = Callable[[TaskPayload], "str | None"]
GateDecider = Callable[[str, TaskPayload], bool]
EventHook = Callable[[StreamEvent], list[StreamEvent]]
FinalTextHook = Callable[[str, TaskPayload, str, bool], list[StreamEvent]]


def _no_extra_events(_event: StreamEvent) -> list[StreamEvent]:
    return []


def _no_final_events(
    _task: str, _task_input: TaskPayload, _final_text: str, _task_failed: bool
) -> list[StreamEvent]:
    return []


def _accept_all_tools(tools: list[Any]) -> list[Any]:
    return tools


def _no_input_error(_task_input: TaskPayload) -> str | None:
    return None


def _never_awaits_gate(_task: str, _task_input: TaskPayload) -> bool:
    return False


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Everything one agent contributes to the shared runtime.

    Attributes:
        agent_key: `"engagement"` / `"workflow"` / `"development"`. Used as
            `trn.trace_session.agent_name`, the local-dev runtime ARN
            fallback (see `config.AgentProcessSettings.resolved_runtime_arn`),
            and the structured-logging `agent` field bound via
            `fde_mcp.logging.bind_session`.
        system_prompt: Carried verbatim into the `strands.Agent` -- see the
            agent's own `prompt.py`.
        valid_tasks: The task names this agent accepts in `payload["task"]`.
        build_prompt: `(task, engagement_id, session_id, task_input) ->
            prompt`. All task-specific prompt construction lives here.
        filter_tools: `(all_mcp_tools) -> allowed_tools`. Identity for
            Engagement/Workflow; the Development agent narrows this to its
            read-only allowlist.
        validate_input: `(task_input) -> error_message | None`, checked
            after task/engagement_id validation. Used by the Development
            agent to require `input.workflow_id`.
        awaits_gate: `(task, task_input) -> bool`. Whether THIS invocation
            should camp on the HITL gate(s) any proposal it raised needs,
            rather than returning immediately with `outcome="pending"`.
        on_event: `(event) -> extra_events_to_yield_first`, called for
            every streamed event except `turn_complete` (which
            `run_agent_task` intercepts itself to accumulate
            `final_text`/`provenance`/`proposal_ids`). Used by the Workflow
            agent to run `guardrails.check_workflow` against a `wf_draft`
            call's actual arguments and surface a `guardrail_violation`
            event ahead of the `tool_result` event it's about.
        after_final_text: `(task, task_input, final_text, task_failed) ->
            extra_events`, called once after the turn-level
            `guardrails.check_turn` pass. Used by the Development agent to
            extract and (optionally) write the `scaffold_agent` files block.
        include_proposal_ids_in_done: Whether the final `"done"` event
            includes a `proposal_ids` key. `True` for Engagement/Workflow
            (their whole point is to raise proposals); `False` for
            Development, which preserves that agent's original response
            shape (it never raises proposals, so the key would always be an
            empty list).
    """

    agent_key: str
    system_prompt: str
    valid_tasks: frozenset[str]
    build_prompt: PromptBuilder
    filter_tools: ToolFilter = _accept_all_tools
    validate_input: InputValidator = _no_input_error
    awaits_gate: GateDecider = _never_awaits_gate
    on_event: EventHook = _no_extra_events
    after_final_text: FinalTextHook = _no_final_events
    include_proposal_ids_in_done: bool = True


def _error(message: str) -> StreamEvent:
    return {"type": "error", "message": message}


def _guardrail_event(violations: list[Violation]) -> StreamEvent:
    return {"type": "guardrail_violation", "violations": [v.__dict__ for v in violations]}


async def _gate_wait_events(
    app: Any,
    mcp_client: Any,
    proposal_id: int,
    timeout_s: float,
) -> AsyncIterator[tuple[StreamEvent, hitl.GateWaitResult | None]]:
    """Yield the SSE events for waiting on one proposal's HITL gate(s).

    Paired with the (optional) `GateWaitResult` so `run_agent_task` can fold
    the wait's outcome into the invocation's overall `outcome` without this
    generator needing to know anything about that bookkeeping -- the second
    tuple element is `None` for every event except the final `gate_result`.

    The progress bridge (an `asyncio.Queue` fed by `hitl.await_human_gate`'s
    `on_poll` callback, drained here with a 5s timeout so this loop can also
    notice `gate_task` finishing) is the same shape the original three
    agents each implemented inline; centralising it means a change to how
    gate-wait progress is framed over SSE cannot drift between agents.
    """
    yield ({"type": "status", "message": f"awaiting human gate for proposal {proposal_id}"}, None)

    progress_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _on_poll(status: dict[str, Any]) -> None:
        await progress_queue.put(status)

    gate_task = asyncio.create_task(
        hitl.await_human_gate(app, mcp_client, proposal_id, timeout_s, on_poll=_on_poll)
    )
    while not gate_task.done():
        try:
            status = await asyncio.wait_for(progress_queue.get(), timeout=5.0)
        except TimeoutError:
            continue
        yield ({"type": "gate_poll", "proposal_id": proposal_id, "status": status}, None)

    gate_result = await gate_task
    yield (
        {
            "type": "gate_result",
            "proposal_id": proposal_id,
            "outcome": gate_result.outcome,
            "elapsed_s": gate_result.elapsed_s,
        },
        gate_result,
    )


def _compute_outcome(*, task_failed: bool, proposal_ids: list[int]) -> str:
    """The one outcome rule all three agents share.

    `"rejected"` if the turn itself failed; `"accepted"` if nothing was
    staged for human review (nothing to grade against a human decision, so
    the task simply did what was asked); otherwise `"pending"` until either
    this invocation's own gate wait (see `_gate_wait_events`) or a later
    re-check resolves it. The Development agent never raises proposals, so
    for it this always reduces to `"rejected"`/`"accepted"` -- the same two
    outcomes its original, proposal-less implementation had.
    """
    if task_failed:
        return "rejected"
    if proposal_ids:
        return "pending"
    return "accepted"


def _validate_task_request(
    config: AgentRuntimeConfig, task: Any, engagement_id: Any, task_input: TaskPayload
) -> str | None:
    """The three preconditions every task must pass before any MCP
    connection is opened, folded into one function so `run_agent_task`
    itself only has one branch to take on the result.
    """
    if task not in config.valid_tasks:
        log.info("task_rejected", reason="unknown_task", task=task)
        return f"unknown task {task!r}; must be one of {sorted(config.valid_tasks)}"
    if not engagement_id:
        log.info("task_rejected", reason="missing_engagement_id")
        return "engagement_id is required"
    input_error = config.validate_input(task_input)
    if input_error is not None:
        log.info("task_rejected", reason="invalid_input", detail=input_error)
        return input_error
    return None


@dataclass
class _TurnResult:
    """Accumulator `_stream_turn` fills in as it forwards events, read by
    `run_agent_task` once the generator is exhausted. A dataclass passed by
    reference rather than a return value because `_stream_turn` is itself a
    generator (it must yield events as they arrive, not just at the end).
    """

    final_text: str = ""
    provenance: list[dict[str, Any]] = field(default_factory=list)
    proposal_ids: list[int] = field(default_factory=list)
    failed: bool = False


async def _stream_turn(
    config: AgentRuntimeConfig,
    mcp_client: Any,
    session: tracing.TraceSession,
    agent: Any,
    *,
    prompt: str,
    result: _TurnResult,
) -> AsyncIterator[StreamEvent]:
    """Run one `streaming.run_agent_turn`, forwarding every event except
    `turn_complete` (accumulated into `result` instead) and running
    `config.on_event` ahead of each forwarded event. Isolated from
    `run_agent_task` so that function's own branch/statement count stays
    low enough to read in one pass -- see module docstring.
    """
    try:
        async for event in streaming.run_agent_turn(mcp_client, session, agent, prompt):
            if event["type"] == "turn_complete":
                result.final_text = event["final_text"]
                result.provenance.extend(event["provenance"])
                result.proposal_ids.extend(event["proposal_ids"])
                continue
            for extra in config.on_event(event):
                yield extra
            yield event
    except Exception as exc:
        log.exception("agent_turn_failed")
        result.failed = True
        yield _error(str(exc))


async def _await_all_gates(
    config: AgentRuntimeConfig,
    app: Any,
    mcp_client: Any,
    task: str,
    task_input: TaskPayload,
    *,
    proposal_ids: list[int],
    timeout_s: float,
    outcome: str,
) -> AsyncIterator[tuple[StreamEvent, str]]:
    """Camp on every proposal's gate(s) in turn, if `config.awaits_gate`
    says this invocation should, yielding `(event, running_outcome)` pairs
    so `run_agent_task` can track the outcome without owning the loop
    itself. A no-op async generator (yields nothing) when there is nothing
    to wait on or this task type never waits.
    """
    if not proposal_ids or not config.awaits_gate(task, task_input):
        return
    for proposal_id in proposal_ids:
        async for event, gate_result in _gate_wait_events(app, mcp_client, proposal_id, timeout_s):
            if gate_result is not None:
                if gate_result.outcome == "approved":
                    outcome = "accepted"
                elif gate_result.outcome == "rejected":
                    outcome = "rejected"
                # 'timed_out'/'expired'/'not_found' leave outcome pending --
                # a later re-check resolves it, not this invocation.
            yield event, outcome


async def run_agent_task(
    config: AgentRuntimeConfig,
    app: Any,
    payload: TaskPayload,
    context: Any = None,
) -> AsyncIterator[StreamEvent]:
    """The shared entrypoint body. See module docstring for what this
    factors out and why.
    """
    settings = get_agent_runtime_settings()
    session_id = getattr(context, "session_id", None) or str(uuid.uuid4())
    task = payload.get("task")
    engagement_id = payload.get("engagement_id")
    task_input: TaskPayload = payload.get("input") or {}

    bind_session(session_id, agent=config.agent_key, engagement_id=engagement_id)

    validation_error = _validate_task_request(config, task, engagement_id, task_input)
    if validation_error is not None:
        yield _error(validation_error)
        return
    # `_validate_task_request` has confirmed `task` is one of
    # `config.valid_tasks` (all `str`) and `engagement_id` is truthy; `cast`
    # narrows the `payload.get(...)`-derived `Any` accordingly rather than
    # threading `Any` through every downstream call that expects `str`.
    task = cast(str, task)
    engagement_id = cast(str, engagement_id)

    log.info("task_started", task=task)
    yield {
        "type": "status",
        "message": f"starting {task} for engagement {engagement_id}",
        "session_id": session_id,
    }

    # Forwarded to the local-dev stdio MCP subprocess via
    # mcp_tools._stdio_transport's env passthrough, so fde_mcp's OWN
    # tool-call tracing (fde_mcp.tools._base.emit_trace) knows this
    # session id. Unrelated to this module's own trace writes
    # (tracing.py talks to fde_mcp.db directly, in-process).
    os.environ["FDE_TRACE_SESSION_ID"] = session_id

    # Resolved once, used for both the audit column and the actual Bedrock
    # call -- the proposal's recorded model id and the model that authored
    # it can never disagree (see config.MODEL_PRESETS).
    model_id = resolve_model_id(config.agent_key)

    with mcp_tools.build_mcp_client(settings.gateway) as mcp_client:
        tools = config.filter_tools(mcp_client.list_tools_sync())

        try:
            session = await tracing.start_session(
                mcp_client,
                session_id=session_id,
                engagement_id=engagement_id,
                agent_name=config.agent_key,
                agent_runtime_arn=settings.process.resolved_runtime_arn(config.agent_key),
                model_id=model_id,
                task_kind=task,
                task_input=task_input,
                agent_qualifier=settings.process.agent_qualifier,
            )
        except Exception as exc:
            log.exception("trace_session_start_failed")
            yield _error(f"could not start trace session: {exc}")
            return

        model = BedrockModel(model_id=model_id)
        agent = Agent(model=model, tools=tools, system_prompt=config.system_prompt)
        prompt = config.build_prompt(task, engagement_id, session_id, task_input)

        turn = _TurnResult()
        async for event in _stream_turn(
            config, mcp_client, session, agent, prompt=prompt, result=turn
        ):
            yield event

        if not turn.failed:
            violations = guardrails.check_turn(turn.final_text, session_provenance=turn.provenance)
            if violations:
                yield _guardrail_event(violations)

        for extra in config.after_final_text(task, task_input, turn.final_text, turn.failed):
            yield extra

        outcome = _compute_outcome(task_failed=turn.failed, proposal_ids=turn.proposal_ids)

        if not turn.failed:
            async for event, new_outcome in _await_all_gates(
                config,
                app,
                mcp_client,
                task,
                task_input,
                proposal_ids=turn.proposal_ids,
                timeout_s=settings.process.gate_wait_timeout_s,
                outcome=outcome,
            ):
                outcome = new_outcome
                yield event

        await tracing.end_session(
            mcp_client, session, outcome=outcome, final_output={"text": turn.final_text}
        )
        log.info("task_finished", outcome=outcome, proposal_count=len(turn.proposal_ids))

        done_event: StreamEvent = {"type": "done", "outcome": outcome}
        if config.include_proposal_ids_in_done:
            done_event["proposal_ids"] = turn.proposal_ids
        yield done_event


Handler = Callable[[TaskPayload, Any], AsyncIterator[StreamEvent]]


def create_app(config: AgentRuntimeConfig) -> tuple[BedrockAgentCoreApp, Handler]:
    """Build a ready-to-run `BedrockAgentCoreApp` for `config`.

    Returns `(app, handler)` rather than only `app`: `handler` is the bare
    async-generator coroutine function, registered with `app.entrypoint`
    below but also directly importable and callable from tests without
    going through a live Starlette app -- the same shape the original three
    `agent.py` modules used.
    """
    app = BedrockAgentCoreApp()

    @app.ping
    def ping_status() -> PingStatus:
        """Mirrors AgentCore's own automatic behaviour (`HEALTHY_BUSY` while
        any async task is registered, `HEALTHY` otherwise). Defined
        explicitly -- rather than relying purely on the default -- so the
        health contract is visible and testable here rather than only
        implicit in the SDK; it must never diverge from what
        `add_async_task`/`complete_async_task` already drive automatically.
        """
        info = app.get_async_task_info()
        active_count: int = info["active_count"]
        return PingStatus.HEALTHY_BUSY if active_count > 0 else PingStatus.HEALTHY

    async def handler(payload: TaskPayload, context: Any = None) -> AsyncIterator[StreamEvent]:
        async for event in run_agent_task(config, app, payload, context):
            yield event

    app.entrypoint(handler)
    return app, handler


__all__ = [
    "AgentRuntimeConfig",
    "EventHook",
    "FinalTextHook",
    "GateDecider",
    "Handler",
    "InputValidator",
    "PromptBuilder",
    "StreamEvent",
    "TaskPayload",
    "ToolFilter",
    "create_app",
    "run_agent_task",
]
