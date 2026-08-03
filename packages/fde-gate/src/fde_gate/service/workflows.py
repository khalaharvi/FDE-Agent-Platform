"""service/workflows.py -- listing workflows, and the only path that publishes one.

This module is split across BOTH roles, which is why the two functions look
asymmetric:

* `list_workflows` runs as `fde_prodops` -- product ops browses what it can
  run, and holds SELECT on all of wf (db/010:66).
* `publish` runs as `fde_gate_service`, because publishing is a gated action
  product ops explicitly does not hold (db/010:64). It is reachable at all
  only because `wf.publish_workflow` is SECURITY DEFINER: NO role in the
  platform holds UPDATE on wf.workflow, and that absence is a CI invariant
  (an agent that could flip status to 'published' could publish its own
  invented process). The privilege is EXECUTE on one audited transition
  rather than UPDATE on a table, so the gate service can publish and still
  cannot, say, repoint `pinned_commit_id`.

`wf.assert_faithful` runs inside the function, not here. An unbound step is
a step nobody in the business performs, and a caller that simply forgot to
check must not be able to publish one.
"""

from __future__ import annotations

from typing import Any

from fde_gate.config import get_gate_settings
from fde_gate.http import as_conflict
from fde_gate.rows import fetchall, fetchone
from fde_mcp import db
from fde_mcp.logging import get_logger
from fde_mcp.playbook import (
    PROCESS_FLOW_SQL,
    STEPS_SQL,
    WORKFLOW_SQL,
    render_playbook,
    sort_bindings,
)

log = get_logger(__name__)

__all__ = ["get_playbook", "get_workflow", "list_workflows", "publish"]

_MAX_LIMIT = 500

# The workflow/step queries come from `fde_mcp.playbook`, which owns both the
# document and the rows it is rendered from. `get_workflow` uses them too: the
# console page, the JSON API and the Markdown export are three views of one row
# set, and a column that existed on only some of them would be a field that
# silently appears depending on which surface you asked.
#
# The role is this module's decision, not the query's -- these run as
# `fde_prodops`, the MCP tool runs the same text as `fde_agent`.


async def list_workflows(
    *,
    status: str | None = "published",
    engagement_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Workflows, defaulting to the published ones product ops actually runs.

    docs/10 §2 tells the operator to filter to "published" before starting
    anything, so that is the default rather than something to remember.
    `status=None` returns every status, which is what the authoring view of
    the console needs to offer a publish button on a draft.
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    async with (
        db.tool_transaction(role=get_gate_settings().gate.prodops_role) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            """
            SELECT w.workflow_id, w.workflow_uuid, w.engagement_id, w.slug, w.version,
                   w.title, w.description, w.status::text AS status, w.root_process_key,
                   w.pinned_commit_id, w.pinned_digest, w.runnable_by, w.autonomy_level,
                   w.authored_by, w.published_by, w.published_at, w.deprecated_at,
                   w.created_at,
                   (SELECT count(*) FROM wf.step s WHERE s.workflow_id = w.workflow_id)
                     AS step_count,
                   (SELECT count(*) FROM wf.step s
                     WHERE s.workflow_id = w.workflow_id
                       AND s.kind <> 'notify'
                       AND NOT EXISTS (SELECT 1 FROM wf.step_binding b
                                        WHERE b.step_id = s.step_id)) AS unbound_steps
              FROM wf.workflow w
             WHERE (%(status)s::text IS NULL
                    OR w.status = %(status)s::wf.workflow_status)
               AND (%(eng)s::uuid IS NULL OR w.engagement_id = %(eng)s::uuid)
             ORDER BY w.engagement_id, w.slug, w.version DESC
             LIMIT %(limit)s
            """,
            {"status": status, "eng": engagement_id, "limit": limit},
        )
        workflows = await fetchall(cur)

    return {
        "status": status,
        "engagement_id": engagement_id,
        "returned": len(workflows),
        "workflows": workflows,
    }


async def get_workflow(workflow_id: int) -> dict[str, Any]:
    """One workflow with its ordered steps and their faithfulness bindings."""
    async with (
        db.tool_transaction(role=get_gate_settings().gate.prodops_role) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(WORKFLOW_SQL, {"wid": workflow_id})
        workflow = await fetchone(cur)
        if workflow is None:
            return {"error": f"workflow {workflow_id} not found"}

        await cur.execute(STEPS_SQL, {"wid": workflow_id})
        workflow["steps"] = await fetchall(cur)

    return workflow


async def get_playbook(workflow_id: int) -> dict[str, Any]:
    """The workflow as a readable procedure: the rows, and the Markdown of them.

    One transaction for all three reads, so the process walk and the pinned
    workflow it is shown beside cannot come from either side of a merge that
    landed while the page was being built.

    The workflow detail page and `GET /api/workflows/{id}/playbook.md` both
    call this, and the MCP tool renders from the same function against the
    same rows -- three surfaces, one document, by construction rather than by
    three sets of formatting decisions that agree today.
    """
    async with (
        db.tool_transaction(role=get_gate_settings().gate.prodops_role) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(WORKFLOW_SQL, {"wid": workflow_id})
        workflow = await fetchone(cur)
        if workflow is None:
            return {"error": f"workflow {workflow_id} not found"}

        await cur.execute(STEPS_SQL, {"wid": workflow_id})
        steps = await fetchall(cur)

        await cur.execute(
            PROCESS_FLOW_SQL,
            {"eng": workflow["engagement_id"], "key": workflow["root_process_key"]},
        )
        process_flow = await fetchall(cur)

    # Sorted once, here, so the page's table and the document's citation list
    # are the same rows in the same order rather than two independent renders
    # of one query.
    for step in steps:
        step["bindings"] = sort_bindings(step.get("bindings") or [])
    bindings = {str(step["step_key"]): step["bindings"] for step in steps}
    return {
        "workflow": workflow,
        "steps": steps,
        "process_flow": process_flow,
        "markdown": render_playbook(workflow, steps, bindings, process_flow),
    }


async def publish(workflow_id: int, principal: str) -> dict[str, Any]:
    """Publish a draft/review workflow. The only publish path in the platform.

    Both refusals this can produce are 409s, not 400s: an already-published
    workflow and an unfaithful one are both statements about the target, not
    about the request. The `assert_faithful` message names the unbound step
    keys, and it reaches the caller verbatim -- that list is the entire
    instruction for how to fix it.
    """
    async with (
        db.tool_transaction(role=get_gate_settings().gate.gate_role) as conn,
        conn.cursor() as cur,
    ):
        with as_conflict():
            await cur.execute(
                "SELECT * FROM wf.publish_workflow(%(wid)s, %(principal)s)",
                {"wid": workflow_id, "principal": principal},
            )
            workflow = await fetchone(cur)

    log.info("workflow_published", workflow_id=workflow_id, principal=principal)
    return {"workflow": workflow}
