"""runner.py -- the workflow runner. Two short transactions per step, never one long one.

    txn 1 (fde_prodops):  wf.begin_step(run_id)     -> commits; the attempt is
                                                       now visible as 'running'
      << execute the step -- no transaction open, may take minutes >>
    txn 2 (fde_prodops):  wf.complete_step(...) or wf.fail_step(...)

Holding a transaction across an agent invocation would pin a connection and
a snapshot for the length of a model call, and a Lambda that dies mid-call
would leave the row locked until the connection was reaped. Splitting it
leaves a different problem -- a crash between the two transactions strands a
`running` attempt -- and that one has an owner: `wf.timeout_steps()` fails it
at the step's `timeout_seconds` and routes it through `on_failure`. The crash
self-heals; it does not need the runner to be reliable.

Concurrency is the database's job too. `wf.begin_step` takes the run's row
lock and refuses if any attempt is already open, so the one-minute tick and
an operator hammering "advance" cannot double-invoke an agent: one wins, the
other gets a RAISE naming the open step.

What the runner does NOT decide
--------------------------------
Retries, skips, escalations, what "the next step" means, and whether a run is
finished are all `wf.fail_step` / `wf.complete_step` / `wf.advance_cursor`.
This module chooses WHAT to execute and reports what happened. That division
is why a run advanced by the API, by the tick, and by an operator's console
click cannot end up in three different states.

The 29-second problem
----------------------
API Gateway gives a synchronous request 29 seconds; an agent step can take
minutes. So an advance triggered by a request runs the cheap kinds inline
(`decision`, `notify`, and parking a `human` step are all milliseconds) and,
when the next step is `agent` or `tool`, asynchronously re-invokes this same
Lambda and returns 202. The async invocation gets the full Lambda budget. A
lost async invoke is not a stuck run: the tick re-advances anything left with
no open step.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from fde_gate.config import get_gate_settings
from fde_gate.executors import StepExecutionError, default_executors
from fde_gate.http import GateError, as_conflict
from fde_gate.rows import fetchall, fetchone
from fde_gate.service import proposals
from fde_mcp import db
from fde_mcp.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fde_gate.executors import StepExecutor

log = get_logger(__name__)

__all__ = [
    "SelfInvoker",
    "advance",
    "default_invoker",
    "expiry_sweep",
    "tick",
]

# The kinds that can take longer than an API Gateway request is allowed to.
_SLOW_KINDS = frozenset({"agent", "tool"})

# A ceiling on how many steps one advance() will execute before returning.
# Not a safety net for a cycle in the workflow -- `wf.advance_cursor` moves
# strictly forward by ordinal, and a decision branch can only jump within the
# same workflow -- but for the case a workflow author DOES author a loop with
# branches. Hitting it returns normally; the tick picks the run up again.
_MAX_STEPS_PER_ADVANCE = 50

# How many idle runs one tick will advance. The tick fires every minute, so a
# backlog drains at this rate rather than one invocation trying to do all of
# it and timing out halfway through with no record of where it got to.
_MAX_RUNS_PER_TICK = 25

SelfInvoker = Callable[[int], Awaitable[None]]


async def default_invoker() -> SelfInvoker | None:
    """The async self-invoke, or None when this process should run inline.

    None whenever `FDE_GATE_FUNCTION_NAME` is unset, which covers every local
    run and the whole test suite -- so "no Lambda configured" means "execute
    it here", not "silently do nothing".
    """
    settings = get_gate_settings().gate
    if not settings.self_function_name:
        return None

    import boto3  # noqa: PLC0415 -- only the deployed path needs the client

    client = boto3.client("lambda", region_name=settings.aws_region)
    function_name = settings.self_function_name

    async def _invoke(run_id: int) -> None:
        def _call() -> None:
            client.invoke(
                FunctionName=function_name,
                InvocationType="Event",
                Payload=json.dumps({"source": "fde.gate.advance", "run_id": run_id}).encode(),
            )

        await asyncio.to_thread(_call)
        log.info("run_advance_dispatched", run_id=run_id, function_name=function_name)

    return _invoke


def _prodops_role() -> str:
    return get_gate_settings().gate.prodops_role


_RUN_SQL = """
SELECT r.run_id, r.workflow_id, r.engagement_id, r.status::text AS status, r.context,
       r.current_step_id, r.runtime_session_id, r.agent_runtime_arn, r.error,
       r.started_at, r.finished_at
  FROM wf.run r WHERE r.run_id = %(rid)s
"""

_STEP_SQL = """
SELECT step_id, workflow_id, step_key, ordinal, kind::text AS kind, title, instruction,
       tool_name, tool_args, human_prompt, human_schema, branches, sor_adapter_key,
       sor_write_op, requires_human, timeout_seconds, on_failure
  FROM wf.step WHERE step_id = %(sid)s
"""


async def _load(run_id: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The run and its current step, in one short read transaction."""
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        await cur.execute(_RUN_SQL, {"rid": run_id})
        run = await fetchone(cur)
        if run is None or run["current_step_id"] is None:
            return run, None
        await cur.execute(_STEP_SQL, {"sid": run["current_step_id"]})
        step = await fetchone(cur)
    return run, step


async def _begin_step(run_id: int) -> dict[str, Any]:
    """Open the current step for execution.

    Wrapped in `as_conflict()` so a caller racing the tick gets a 409 with
    the SQL's own message ("run N already has an open step; complete, fail or
    respond to it before beginning another") rather than a 500. That is not
    an error condition in the system -- it is two schedulers doing their job.
    """
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        with as_conflict():
            await cur.execute("SELECT * FROM wf.begin_step(%(rid)s)", {"rid": run_id})
            row = await fetchone(cur)
    if row is None:  # pragma: no cover -- the function raises rather than returning NULL
        raise GateError(500, f"wf.begin_step({run_id}) returned no row")
    return row


async def _await_human(run_step_id: int, principal: str | None) -> dict[str, Any] | None:
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT * FROM wf.await_human(%(rsid)s, %(principal)s)",
            {"rsid": run_step_id, "principal": principal},
        )
        return await fetchone(cur)


async def _complete_step(
    run_step_id: int, output: dict[str, Any], goto: str | None
) -> dict[str, Any] | None:
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT * FROM wf.complete_step(%(rsid)s, %(out)s::jsonb, %(goto)s)
            """,
            {"rsid": run_step_id, "out": Jsonb(output), "goto": goto},
        )
        return await fetchone(cur)


async def _fail_step(run_step_id: int, error: dict[str, Any]) -> dict[str, Any] | None:
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT * FROM wf.fail_step(%(rsid)s, %(err)s::jsonb, %(max)s)",
            {
                "rsid": run_step_id,
                "err": Jsonb(error),
                "max": get_gate_settings().gate.max_step_attempts,
            },
        )
        return await fetchone(cur)


def _assignee(step: Mapping[str, Any]) -> str | None:
    """Who a `human` step is parked on, from `human_schema.assignee`.

    None means "whoever is on shift" -- `run_step_awaiting_idx` indexes NULL
    `awaiting_principal` too, and `service.runs.awaiting_steps` shows those
    rows to every operator. Assigning by default would be worse: a step
    addressed to one person who is on holiday is invisible to everyone else.
    """
    schema = step.get("human_schema")
    if isinstance(schema, dict):
        assignee = schema.get("assignee")
        if isinstance(assignee, str) and assignee:
            return assignee
    return None


async def advance(
    run_id: int,
    *,
    executors: Mapping[str, StepExecutor] | None = None,
    invoker: SelfInvoker | None = None,
) -> dict[str, Any]:
    """Drive a run forward until it stops needing this process.

    Returns a status envelope describing where it left the run:
    `succeeded`/`failed`/`cancelled` for a finished run, `awaiting_human`
    when it parked on a person, or `running` when it handed the rest to an
    async invocation.

    `executors` and `invoker` are injected rather than looked up so the whole
    loop is exercisable with fakes and no AWS.
    """
    table = dict(default_executors()) if executors is None else dict(executors)
    executed: list[dict[str, Any]] = []

    for _ in range(_MAX_STEPS_PER_ADVANCE):
        run, step = await _load(run_id)
        if run is None:
            raise GateError(404, f"run {run_id} not found")

        status = str(run["status"])
        if status in ("succeeded", "failed", "cancelled"):
            return _envelope(run_id, status, executed, run=run)
        if status == "awaiting_human":
            return _envelope(run_id, "awaiting_human", executed, run=run)
        if step is None:
            # A non-terminal run with no current step is a state the SQL
            # transitions do not produce; report it rather than looping.
            return _envelope(run_id, status, executed, run=run, detail="run has no current step")

        kind = str(step["kind"])

        if kind == "human":
            opened = await _begin_step(run_id)
            await _await_human(int(opened["run_step_id"]), _assignee(step))
            executed.append(
                {"step_key": step["step_key"], "kind": kind, "result": "awaiting_human"}
            )
            log.info(
                "wf_step_awaiting_human",
                run_id=run_id,
                step_key=step["step_key"],
                awaiting_principal=_assignee(step),
            )
            return _envelope(run_id, "awaiting_human", executed)

        if kind in _SLOW_KINDS and invoker is not None:
            await invoker(run_id)
            return _envelope(
                run_id,
                "running",
                executed,
                detail=f"step {step['step_key']} is executing asynchronously",
            )

        executor = table.get(kind)
        if executor is None:
            raise GateError(500, f"no executor registered for step kind {kind!r}")

        opened = await _begin_step(run_id)
        run_step_id = int(opened["run_step_id"])
        try:
            output = await executor.execute(step, run)
        except StepExecutionError as failure:
            await _fail_step(run_step_id, failure.detail)
            executed.append(
                {
                    "step_key": step["step_key"],
                    "kind": kind,
                    "result": "failed",
                    "error": failure.detail,
                }
            )
            log.info(
                "wf_step_failed",
                run_id=run_id,
                step_key=step["step_key"],
                on_failure=step["on_failure"],
                attempt=opened["attempt"],
            )
            continue
        except Exception as exc:
            # An executor that raised something other than StepExecutionError is a
            # bug in the executor, but the run must not be left with an open
            # attempt because of it -- fail the step so on_failure runs and
            # the traceback reaches the operator as the attempt's error.
            detail = {"error": f"executor raised {type(exc).__name__}: {exc}", "kind": kind}
            await _fail_step(run_step_id, detail)
            log.exception("wf_step_executor_crashed", run_id=run_id, step_key=step["step_key"])
            executed.append(
                {"step_key": step["step_key"], "kind": kind, "result": "failed", "error": detail}
            )
            continue

        goto = str(output["branch"]) if kind == "decision" and "branch" in output else None
        await _complete_step(run_step_id, output, goto)
        executed.append(
            {"step_key": step["step_key"], "kind": kind, "result": "succeeded", "goto": goto}
        )

    log.warning("wf_advance_step_budget_exhausted", run_id=run_id, steps=len(executed))
    return _envelope(
        run_id,
        "running",
        executed,
        detail=f"stopped after {_MAX_STEPS_PER_ADVANCE} steps; the tick will continue this run",
    )


def _envelope(
    run_id: int,
    status: str,
    executed: list[dict[str, Any]],
    *,
    run: dict[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"run_id": run_id, "status": status, "executed": executed}
    if detail is not None:
        body["detail"] = detail
    if run is not None:
        body["run"] = run
    return body


async def tick(*, executors: Mapping[str, StepExecutor] | None = None) -> dict[str, Any]:
    """The one-minute sweep: time out overdue steps, then advance idle runs.

    Order matters. Timing out first turns a stranded attempt into a failure
    that `on_failure` has already routed -- possibly back to `running` with
    the cursor unmoved -- so the same tick can then pick the run up and retry
    it, instead of leaving it for the next minute.

    Runs are advanced INLINE here (no invoker): this invocation already has
    the full Lambda budget, and bouncing through another async invoke would
    only add a failure mode.
    """
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        await cur.execute("SELECT wf.timeout_steps() AS timed_out")
        row = await fetchone(cur)
        timed_out = 0 if row is None else int(row["timed_out"])

        await cur.execute(
            """
            SELECT r.run_id
              FROM wf.run r
             WHERE r.status IN ('pending','running')
               AND r.current_step_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM wf.run_step rs
                                WHERE rs.run_id = r.run_id
                                  AND rs.status IN ('running','awaiting_human'))
             ORDER BY r.started_at
             LIMIT %(limit)s
            """,
            {"limit": _MAX_RUNS_PER_TICK},
        )
        idle = [int(r["run_id"]) for r in await fetchall(cur)]

    advanced: list[dict[str, Any]] = []
    for run_id in idle:
        try:
            advanced.append(await advance(run_id, executors=executors))
        except GateError as exc:
            # Almost always "run already has an open step": an API-triggered
            # advance won the race between this tick's SELECT and its
            # begin_step. Nothing to do, and nothing wrong.
            log.info("wf_tick_run_skipped", run_id=run_id, reason=exc.message)
        except pg_errors.Error:
            log.exception("wf_tick_run_failed", run_id=run_id)

    log.info("wf_tick", timed_out=timed_out, idle=len(idle), advanced=len(advanced))
    return {"timed_out": timed_out, "idle_runs": len(idle), "advanced": advanced}


async def expiry_sweep() -> dict[str, Any]:
    """The hourly sweep: expire proposals past their SLA.

    Runs as the gate role, not prodops -- it writes `hitl.proposal.status`.
    See `service.proposals.expire` for why it deliberately leaves the
    training label alone.
    """
    return await proposals.expire()
