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

log = get_logger(__name__)

__all__ = ["get_workflow", "list_workflows", "publish"]

_MAX_LIMIT = 500


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
        await cur.execute(
            """
            SELECT workflow_id, workflow_uuid, engagement_id, slug, version, title,
                   description, status::text AS status, root_process_key,
                   pinned_commit_id, pinned_digest, runnable_by, autonomy_level,
                   authored_by, published_by, published_at, deprecated_at, created_at
              FROM wf.workflow WHERE workflow_id = %(wid)s
            """,
            {"wid": workflow_id},
        )
        workflow = await fetchone(cur)
        if workflow is None:
            return {"error": f"workflow {workflow_id} not found"}

        await cur.execute(
            """
            SELECT s.step_id, s.step_key, s.ordinal, s.kind::text AS kind, s.title,
                   s.instruction, s.tool_name, s.tool_args, s.human_prompt,
                   s.human_schema, s.branches, s.sor_adapter_key, s.sor_write_op,
                   s.requires_human, s.timeout_seconds, s.on_failure,
                   coalesce(
                     (SELECT jsonb_agg(jsonb_build_object(
                               'subject_kind', b.subject_kind,
                               'subject_key',  b.subject_key,
                               'relation',     b.relation,
                               'pinned_label', b.pinned_label)
                             ORDER BY b.binding_id)
                        FROM wf.step_binding b WHERE b.step_id = s.step_id),
                     '[]'::jsonb) AS bindings
              FROM wf.step s
             WHERE s.workflow_id = %(wid)s
             ORDER BY s.ordinal
            """,
            {"wid": workflow_id},
        )
        workflow["steps"] = await fetchall(cur)

    return workflow


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
