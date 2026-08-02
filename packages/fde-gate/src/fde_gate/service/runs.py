"""service/runs.py -- starting, reading, answering and cancelling workflow runs.

Runs as `fde_prodops`: SELECT on all of wf, INSERT/UPDATE on wf.run and
wf.run_step, and EXECUTE on the run transitions (db/010:66-67, db/013's
grants section). Notably absent from that list is `wf.publish_workflow` --
product ops runs workflows; publishing one is somebody else's authority.

Nothing here interprets `on_failure`, decides what "next step" means, or
flips a run status. All of that is in db/013, once, so the API path, the
console path and the EventBridge tick cannot disagree about it. What this
module owns is reading rows back in a shape a person can act on.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from fde_gate.config import get_gate_settings
from fde_gate.http import as_conflict
from fde_gate.rows import fetchall, fetchone
from fde_mcp import db
from fde_mcp.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "ACTIVE_STATUSES",
    "awaiting_steps",
    "cancel",
    "get_run",
    "list_runs",
    "respond",
    "start_run",
]

# The three non-terminal run states. `pending` and `running` are what the
# tick re-advances; `awaiting_human` is deliberately NOT -- a run waiting on
# a person is not stuck, and re-advancing it would be the runner arguing
# with the operator.
ACTIVE_STATUSES: tuple[str, ...] = ("pending", "running", "awaiting_human")

_MAX_LIMIT = 500


def _prodops_role() -> str:
    return get_gate_settings().gate.prodops_role


async def list_runs(
    *,
    statuses: tuple[str, ...] | None = None,
    engagement_id: str | None = None,
    workflow_id: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Runs, most recent first, using wf.run's `run_queue_idx`."""
    limit = max(1, min(limit, _MAX_LIMIT))
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT r.run_id, r.run_uuid, r.workflow_id, r.engagement_id,
                   r.status::text AS status, r.started_by, r.input, r.context,
                   r.runtime_session_id, r.agent_runtime_arn, r.current_step_id,
                   r.error, r.started_at, r.finished_at,
                   w.slug AS workflow_slug, w.title AS workflow_title,
                   s.step_key AS current_step_key, s.kind::text AS current_step_kind
              FROM wf.run r
              JOIN wf.workflow w ON w.workflow_id = r.workflow_id
              LEFT JOIN wf.step s ON s.step_id = r.current_step_id
             WHERE (%(statuses)s::text[] IS NULL
                    OR r.status = ANY (%(statuses)s::wf.run_status[]))
               AND (%(eng)s::uuid IS NULL OR r.engagement_id = %(eng)s::uuid)
               AND (%(wid)s::bigint IS NULL OR r.workflow_id = %(wid)s)
             ORDER BY r.started_at DESC, r.run_id DESC
             LIMIT %(limit)s
            """,
            {
                "statuses": None if statuses is None else list(statuses),
                "eng": engagement_id,
                "wid": workflow_id,
                "limit": limit,
            },
        )
        runs = await fetchall(cur)

    return {
        "statuses": None if statuses is None else list(statuses),
        "engagement_id": engagement_id,
        "returned": len(runs),
        "runs": runs,
    }


async def get_run(run_id: int) -> dict[str, Any]:
    """One run with every attempt of every step it has taken.

    Attempts are returned in full, not collapsed to the latest: docs/10 §2
    tells an operator to read the error message, and a `retry` policy means
    the interesting error is usually on an earlier attempt than the one
    currently open.
    """
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT r.run_id, r.run_uuid, r.workflow_id, r.engagement_id,
                   r.status::text AS status, r.started_by, r.input, r.context,
                   r.runtime_session_id, r.agent_runtime_arn, r.current_step_id,
                   r.error, r.started_at, r.finished_at,
                   w.slug AS workflow_slug, w.title AS workflow_title,
                   w.autonomy_level, w.runnable_by
              FROM wf.run r
              JOIN wf.workflow w ON w.workflow_id = r.workflow_id
             WHERE r.run_id = %(rid)s
            """,
            {"rid": run_id},
        )
        run = await fetchone(cur)
        if run is None:
            return {"error": f"run {run_id} not found"}

        await cur.execute(
            """
            SELECT rs.run_step_id, rs.step_id, rs.attempt, rs.status::text AS status,
                   rs.input, rs.output, rs.awaiting_principal, rs.human_response,
                   rs.responded_by, rs.responded_at, rs.error, rs.started_at,
                   rs.finished_at,
                   s.step_key, s.ordinal, s.kind::text AS kind, s.title, s.instruction,
                   s.human_prompt, s.human_schema, s.on_failure, s.timeout_seconds,
                   s.tool_name, s.sor_adapter_key
              FROM wf.run_step rs
              JOIN wf.step s ON s.step_id = rs.step_id
             WHERE rs.run_id = %(rid)s
             ORDER BY s.ordinal, rs.attempt
            """,
            {"rid": run_id},
        )
        run["steps"] = await fetchall(cur)

        # The steps the workflow still contains but this run has not reached.
        await cur.execute(
            """
            SELECT s.step_id, s.step_key, s.ordinal, s.kind::text AS kind, s.title,
                   s.instruction, s.on_failure, s.timeout_seconds
              FROM wf.step s
             WHERE s.workflow_id = %(wid)s
             ORDER BY s.ordinal
            """,
            {"wid": run["workflow_id"]},
        )
        run["workflow_steps"] = await fetchall(cur)

    run["awaiting"] = [s for s in run["steps"] if s["status"] == "awaiting_human"]
    return run


async def awaiting_steps(principal: str, *, limit: int = 100) -> dict[str, Any]:
    """Run steps parked on a human -- the other half of the review queue.

    Uses `run_step_awaiting_idx`, the partial index on
    (awaiting_principal, started_at) WHERE status = 'awaiting_human'. A NULL
    `awaiting_principal` means "whoever is on shift", so it is shown to
    everyone rather than to nobody.
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT rs.run_step_id, rs.run_id, rs.attempt, rs.awaiting_principal,
                   rs.input, rs.started_at,
                   s.step_key, s.kind::text AS kind, s.title, s.instruction,
                   s.human_prompt, s.human_schema,
                   r.engagement_id, r.status::text AS run_status,
                   w.workflow_id, w.slug AS workflow_slug, w.title AS workflow_title,
                   (rs.input ? 'escalated_error') AS escalated
              FROM wf.run_step rs
              JOIN wf.step s ON s.step_id = rs.step_id
              JOIN wf.run r ON r.run_id = rs.run_id
              JOIN wf.workflow w ON w.workflow_id = r.workflow_id
             WHERE rs.status = 'awaiting_human'
               AND (rs.awaiting_principal IS NULL OR rs.awaiting_principal = %(me)s)
             ORDER BY rs.started_at
             LIMIT %(limit)s
            """,
            {"me": principal, "limit": limit},
        )
        steps = await fetchall(cur)

    return {"principal": principal, "returned": len(steps), "awaiting_steps": steps}


async def start_run(
    workflow_id: int,
    principal: str,
    *,
    run_input: dict[str, Any] | None = None,
    groups: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Start a run of a PUBLISHED workflow.

    Both refusals are 409s. "workflow N is draft, not published" and
    "alice may not run workflow N (runnable_by = {prodops})" are statements
    about the target and the caller's standing, and docs/10 §6 lists them as
    the two things an operator sees when a workflow will not start -- so the
    messages reach the caller verbatim.
    """
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        with as_conflict():
            await cur.execute(
                """
                SELECT * FROM wf.start_run(%(wid)s, %(principal)s, %(input)s::jsonb,
                                           %(groups)s::text[])
                """,
                {
                    "wid": workflow_id,
                    "principal": principal,
                    "input": Jsonb(run_input or {}),
                    "groups": list(groups),
                },
            )
            run = await fetchone(cur)

    log.info(
        "run_started",
        workflow_id=workflow_id,
        principal=principal,
        run_id=None if run is None else run.get("run_id"),
    )
    return {"run": run}


async def respond(
    run_step_id: int,
    principal: str,
    action: str,
    *,
    response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Answer a step waiting on a human: approve / retry / skip / abort.

    One entry point for both flavours of `awaiting_human` -- a `human` step
    the workflow author put there, and an escalated attempt parked by
    `wf.fail_step`. Which actions are legal for which flavour is decided in
    SQL (approve is rejected on an escalated non-human step, with a message
    telling the operator to use retry or skip instead).
    """
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        with as_conflict():
            await cur.execute(
                """
                SELECT * FROM wf.respond_human(%(rsid)s, %(principal)s,
                                               %(response)s::jsonb, %(action)s)
                """,
                {
                    "rsid": run_step_id,
                    "principal": principal,
                    "response": Jsonb(response or {}),
                    "action": action,
                },
            )
            run = await fetchone(cur)

    log.info("run_step_answered", run_step_id=run_step_id, principal=principal, action=action)
    return {"run": run}


async def cancel(run_id: int, principal: str) -> dict[str, Any]:
    """Cancel a non-terminal run and every open step under it."""
    async with db.tool_transaction(role=_prodops_role()) as conn, conn.cursor() as cur:
        with as_conflict():
            await cur.execute(
                "SELECT * FROM wf.cancel_run(%(rid)s, %(principal)s)",
                {"rid": run_id, "principal": principal},
            )
            run = await fetchone(cur)

    log.info("run_cancelled", run_id=run_id, principal=principal)
    return {"run": run}
