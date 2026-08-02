"""The human-gate wait helper: `await_human_gate`.

Why this is its own module, and why the polling loop is shaped the way it is
----------------------------------------------------------------------------
"Agents propose, humans dispose" (see `db/004_hitl_gates.sql`'s module
docstring) means that once an agent calls `kg_submit_proposal` or drafts a
workflow that needs sign-off, the agent's only remaining job is to wait for
`hitl.gates_satisfied` to become true (or the proposal's SLA to expire) and
then react. That wait can legitimately take hours to days -- `hitl.
gate_policy.sla_hours` defaults to 72 -- which collides directly with two
AgentCore Runtime constraints on the container this code runs in:

1. **15-minute ping reaping.** If the runtime's `/ping` handler stops
   responding (or keeps reporting `HEALTHY` while nothing is actually
   happening, which the platform can't distinguish from a hung process) for
   15 minutes, AgentCore reaps the session. A naive `await asyncio.sleep()`
   loop *on the same task that serves `/ping`* would starve the ping
   handler under any framework that isn't running a separate event loop per
   request, and even where it wouldn't literally block ping, reporting
   `HEALTHY` for hours while genuinely waiting on a human is the wrong
   signal -- AgentCore's automatic `HEALTHY_BUSY` behavior while an async
   task is registered exists precisely so the platform can tell "the agent
   is alive and working" apart from "the agent is alive and idle", and a
   HITL wait is neither -- it's alive and *blocked on someone else*, which
   `HEALTHY_BUSY` communicates correctly (do not reap, but also do not
   expect a response soon).

2. **8-hour max session lifetime.** A 72-hour SLA gate cannot be waited out
   inside a single AgentCore session at all. This module's polling loop has
   its own `timeout_s` ceiling for exactly that reason: `await_human_gate`
   is meant to be called with a timeout well inside the 8-hour ceiling (the
   Workflow/Engagement agent's caller decides how long to camp on this
   invocation before giving up and returning control -- e.g. "check back in
   10 minutes" -- rather than this module trying to be clever about
   spanning multiple sessions itself).

The concrete mechanism: `add_async_task` is called ONCE, synchronously, the
moment we start waiting -- this is what flips the runtime's automatic ping
status to `HEALTHY_BUSY` for as long as the task is registered, per the
verified AgentCore facts this module was built against. The actual polling
(`kg_proposal_status` on a backoff schedule) runs on a plain `await`ed
coroutine under `asyncio.wait_for`, NOT via a blocking `time.sleep` -- so the
event loop that also serves `GET /ping` (and would independently keep
reporting `HEALTHY` moment-to-moment regardless of the async task, per
AgentCore's own health-check wiring) is never starved. `complete_async_task`
is called exactly once, in a `finally`, regardless of whether the gate
resolved, was rejected, or the wait timed out -- an async task left
registered after this function returns would keep the runtime pinned at
`HEALTHY_BUSY` for a wait that is, from the runtime's point of view, already
over.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fde_mcp.logging import get_logger

from .tracing import _extract_tool_json  # shared MCP-result unwrapper

log = get_logger(__name__)

GateWaitOutcome = Literal["approved", "rejected", "expired", "timed_out", "not_found"]
OnPollCallback = Callable[[dict[str, Any]], Awaitable[None]]

# Backoff schedule for polling kg_proposal_status. Starts fast (a human
# might clear a trivial ontology gate in seconds during a live working
# session) and backs off to a ceiling so a multi-day SLA wait does not spam
# the database or the Gateway. Never polls faster than once every 5s or
# slower than once every 5 minutes.
_POLL_INITIAL_S = 5.0
_POLL_MAX_S = 300.0
_POLL_BACKOFF_FACTOR = 1.6


@dataclass
class GateWaitResult:
    outcome: GateWaitOutcome
    proposal_id: int
    elapsed_s: float
    last_status: dict[str, Any] | None = None
    detail: str | None = None


async def await_human_gate(
    app: Any,
    mcp_client: Any,
    proposal_id: int,
    timeout_s: float,
    *,
    poll_initial_s: float = _POLL_INITIAL_S,
    poll_max_s: float = _POLL_MAX_S,
    poll_backoff_factor: float = _POLL_BACKOFF_FACTOR,
    on_poll: OnPollCallback | None = None,
) -> GateWaitResult:
    """Wait for `hitl.proposal_id` to clear every required gate, be
    rejected, or expire -- without blocking the AgentCore event loop.

    `app` is the `BedrockAgentCoreApp` instance the entrypoint decorated
    with `@app.entrypoint`; `mcp_client` is a live (already-`__enter__`'d)
    Strands `MCPClient` this coroutine can call `kg_proposal_status` on.

    Returns a `GateWaitResult` rather than raising for every non-approved
    outcome -- a rejected or expired proposal is an expected, first-class
    result an FDE agent must handle (e.g. by re-drafting), not an exceptional
    condition. This only raises if `app.add_async_task`/`complete_async_task`
    themselves fail, which indicates the runtime SDK contract is broken
    rather than anything about the proposal.

    `on_poll`, if given, is awaited with the raw `kg_proposal_status`
    payload after every poll (before evaluating it) -- the Workflow/
    Engagement agent entrypoints use this to stream a progress event back to
    the caller over SSE without this module needing to know anything about
    the streaming response shape.
    """
    task_name = f"hitl:proposal:{proposal_id}"
    task_id = app.add_async_task(task_name, {"proposal_id": proposal_id, "timeout_s": timeout_s})
    log.info("hitl_wait_started", proposal_id=proposal_id, task_id=task_id, timeout_s=timeout_s)

    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            _poll_loop(
                mcp_client,
                proposal_id,
                poll_initial_s=poll_initial_s,
                poll_max_s=poll_max_s,
                poll_backoff_factor=poll_backoff_factor,
                on_poll=on_poll,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        elapsed = time.monotonic() - started
        log.info("hitl_wait_timed_out", proposal_id=proposal_id, elapsed_s=elapsed)
        return GateWaitResult(
            outcome="timed_out",
            proposal_id=proposal_id,
            elapsed_s=elapsed,
            detail=f"no terminal gate state within {timeout_s}s; caller should re-check later",
        )
    finally:
        # MUST run regardless of outcome -- see module docstring. A boolean
        # False return from complete_async_task (task already completed /
        # unknown) is logged, not raised: it means someone else already
        # cleared it, which is a benign race, not a bug in this wait.
        completed = app.complete_async_task(task_id)
        if not completed:
            log.warning(
                "hitl_complete_async_task_no_op",
                task_id=task_id,
                proposal_id=proposal_id,
                reason="already completed or unknown to the runtime",
            )


async def _poll_loop(
    mcp_client: Any,
    proposal_id: int,
    *,
    poll_initial_s: float,
    poll_max_s: float,
    poll_backoff_factor: float,
    on_poll: OnPollCallback | None,
) -> GateWaitResult:
    """Runs as a plain coroutine under `asyncio.wait_for` in the caller.

    Not spawned as a *separate* asyncio Task via `create_task` because
    `wait_for` already gives us cancellation-on-timeout for free and there is
    only ever one waiter per call -- the isolation this module's docstring
    promises is from the runtime's `/ping` handling, which is a property of
    never calling a blocking primitive (no `time.sleep`, no synchronous
    network I/O) anywhere in this loop, not of which asyncio primitive
    schedules it. Every I/O call below is `await`-ed, so the event loop is
    free to service `/ping` (and, for that matter, any other concurrent
    session) between polls and during each poll's own network wait.
    """
    started = time.monotonic()
    delay = poll_initial_s
    poll_count = 0

    while True:
        poll_count += 1
        status = await _fetch_proposal_status(mcp_client, proposal_id)
        elapsed = time.monotonic() - started

        if on_poll is not None:
            try:
                await on_poll(status or {})
            except Exception:
                log.warning("hitl_on_poll_callback_failed", proposal_id=proposal_id, exc_info=True)

        if status is None:
            return GateWaitResult(
                outcome="not_found",
                proposal_id=proposal_id,
                elapsed_s=elapsed,
                detail="kg_proposal_status returned no result",
            )
        if status.get("error"):
            return GateWaitResult(
                outcome="not_found",
                proposal_id=proposal_id,
                elapsed_s=elapsed,
                last_status=status,
                detail=status.get("error"),
            )

        proposal = status.get("proposal") or {}
        proposal_status = proposal.get("status")
        overall_satisfied = status.get("overall_satisfied")

        if proposal_status == "merged" or overall_satisfied is True:
            return GateWaitResult(
                outcome="approved", proposal_id=proposal_id, elapsed_s=elapsed, last_status=status
            )
        if proposal_status == "rejected":
            return GateWaitResult(
                outcome="rejected", proposal_id=proposal_id, elapsed_s=elapsed, last_status=status
            )
        if proposal_status == "expired":
            return GateWaitResult(
                outcome="expired", proposal_id=proposal_id, elapsed_s=elapsed, last_status=status
            )
        if any(g.get("has_blocking_decision") for g in status.get("gates", [])):
            return GateWaitResult(
                outcome="rejected", proposal_id=proposal_id, elapsed_s=elapsed, last_status=status
            )

        log.debug(
            "hitl_poll",
            poll_count=poll_count,
            proposal_id=proposal_id,
            proposal_status=proposal_status,
            elapsed_s=elapsed,
            next_delay_s=delay,
        )
        await asyncio.sleep(delay)
        delay = min(delay * poll_backoff_factor, poll_max_s)


async def _fetch_proposal_status(mcp_client: Any, proposal_id: int) -> dict[str, Any] | None:
    result = await mcp_client.call_tool_async(
        tool_use_id=f"hitl-poll-{proposal_id}-{int(time.time() * 1000)}",
        name="kg_proposal_status",
        arguments={"proposal_id": proposal_id},
    )
    return _extract_tool_json(result)
