"""tools/workflow.py -- faithful workflow authoring.

A "workflow" here is a bounded, human-auditable procedure whose steps must
cite the graph elements they implement/enforce (see `StepBindingSpec`).
`wf_draft` can never publish a workflow -- the `fde_agent` role has no
UPDATE grant on wf.workflow/wf.step, only INSERT (see
db/011_mcp_agent_supplemental_grants.sql) -- so "agents draft, humans
publish" is enforced by the database, not by this code choosing to be
polite about it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from fde_mcp import db
from fde_mcp.config import get_settings
from fde_mcp.playbook import PROCESS_FLOW_SQL, STEPS_SQL, WORKFLOW_SQL, render_playbook
from fde_mcp.tools._base import emit_trace, fetchall, fetchone, now_ms, pg_error_boundary

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["StepBindingSpec", "StepSpec", "register"]

STEP_KINDS = ("agent", "tool", "human", "decision", "sor_write", "notify")
STEP_BINDING_RELATIONS = ("implements", "enforces", "records_to", "depends_on", "measured_by")


class StepBindingSpec(BaseModel):
    """A faithfulness binding: why a workflow step corresponds to a graph
    element. Every non-notify step needs at least one of these or
    `wf.assert_faithful` refuses to let the workflow be considered faithful.
    """

    subject_kind: Literal["node", "edge"]
    subject_key: str
    relation: Literal[STEP_BINDING_RELATIONS]  # type: ignore[valid-type]


class StepSpec(BaseModel):
    """One step of a draft workflow. Mirrors wf.step + its wf.step_binding rows."""

    step_key: str
    ordinal: int
    kind: Literal[STEP_KINDS]  # type: ignore[valid-type]
    title: str
    instruction: str = Field(description="What the operator or agent actually does")
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    human_prompt: str | None = None
    human_schema: dict[str, Any] | None = None
    branches: dict[str, Any] | None = None
    sor_adapter_key: str | None = None
    sor_write_op: str | None = None
    requires_human: bool = False
    timeout_seconds: int = 900
    on_failure: Literal["halt", "retry", "skip", "escalate"] = "halt"
    bindings: list[StepBindingSpec] = Field(
        default_factory=list,
        description="Graph elements this step implements/enforces/etc. Non-notify steps need >= 1.",
    )


@pg_error_boundary
async def wf_list(
    engagement_id: str, status: Literal["draft", "review", "published", "deprecated"] = "published"
) -> dict[str, Any]:
    """List workflows for this engagement in a given `status`
    (default 'published' -- the ones product ops actually runs)."""
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT workflow_id, workflow_uuid, slug, version, title, status,
                       root_process_key, pinned_commit_id, autonomy_level,
                       published_at, deprecated_at
                  FROM wf.workflow
                 WHERE engagement_id = %(eng)s::uuid AND status = %(status)s::wf.workflow_status
                 ORDER BY slug, version DESC
                """,
                {"eng": engagement_id, "status": status},
            )
            rows = await fetchall(cur)
        result = {
            "engagement_id": engagement_id,
            "status": status,
            "returned": len(rows),
            "workflows": rows,
        }
        await emit_trace(conn, "wf_list", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def wf_get(workflow_id: int) -> dict[str, Any]:
    """Full workflow: header, ordered steps with their bindings, and
    `commits_behind` -- how many commits ahead HEAD is of this workflow's
    pinned_commit_id (0 if fully current, null if the engagement has no
    sealed commit at all, which cannot normally happen for a published
    workflow). A nonzero commits_behind does not itself mean the workflow is
    broken -- check drift_list for `stale_pin` signals to see if anything it
    actually binds to changed.
    """
    t0 = now_ms()
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM wf.workflow WHERE workflow_id = %(wid)s", {"wid": workflow_id}
            )
            workflow = await fetchone(cur)
            if workflow is None:
                result: dict[str, Any] = {
                    "error": "workflow not found",
                    "hint": "check workflow_id",
                }
                await emit_trace(conn, "wf_get", result, latency_ms=int(now_ms() - t0))
                return result

            await cur.execute(
                "SELECT * FROM wf.step WHERE workflow_id = %(wid)s ORDER BY ordinal",
                {"wid": workflow_id},
            )
            steps = await fetchall(cur)

            step_ids = [s["step_id"] for s in steps]
            bindings_by_step: dict[int, list[dict[str, Any]]] = {sid: [] for sid in step_ids}
            if step_ids:
                await cur.execute(
                    "SELECT * FROM wf.step_binding WHERE step_id = ANY(%(ids)s)", {"ids": step_ids}
                )
                for b in await fetchall(cur):
                    bindings_by_step.setdefault(b["step_id"], []).append(b)
            for s in steps:
                s["bindings"] = bindings_by_step.get(s["step_id"], [])

            await cur.execute(
                """
                SELECT commit_id FROM kg.commit
                 WHERE engagement_id = %(eng)s::uuid AND status = 'sealed'
                 ORDER BY commit_id DESC LIMIT 1
                """,
                {"eng": workflow["engagement_id"]},
            )
            head = await fetchone(cur)
            commits_behind = (
                head["commit_id"] - workflow["pinned_commit_id"] if head is not None else None
            )

        result = {"workflow": workflow, "steps": steps, "commits_behind": commits_behind}
        await emit_trace(conn, "wf_get", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def wf_export_playbook(workflow_id: int) -> dict[str, Any]:
    """Export a workflow as a playbook: one Markdown document a person can follow.

    Use this whenever the answer is a document rather than a field -- "give
    me the playbook for the discount workflow", "what does this workflow
    actually tell the operator to do", "save this into my notes" -- and use
    it before proposing any change to a workflow, so you are reading the
    same procedure the operator runs. Prefer wf_get when you need to inspect
    one attribute; prefer this when a human is going to read the result.

    Returns `{workflow_id, slug, version, status, markdown}`. The `markdown`
    is a whole document: YAML front matter (slug, version, status, autonomy,
    who may run it, the pinned commit's content digest), a narrative of the
    process the workflow implements, then every step in order with its
    instruction, the question put to a human where there is one, whether it
    stops for a human, what happens on failure, and the graph elements the
    step cites. It is plain Markdown -- paste it into a vault, a wiki or a
    ticket unchanged.

    What the output reflects: the steps are exactly as they stand against
    this workflow's pinned commit, so the same workflow always exports the
    same bytes -- an export is a stable artefact you can diff, not a fresh
    piece of writing. The one exception is the "Process context" section,
    which reads the graph as it stands NOW and says so in the text; it can
    describe a process that has moved on since the pin. Draft workflows
    export too, carrying a banner that they have not passed the publication
    gate and should not be run from the document.

    Read-only: this reads wf.workflow/wf.step/wf.step_binding and the process
    walk, and cannot draft, edit, or publish anything.
    """
    t0 = now_ms()
    # The queries come from fde_mcp.playbook, which owns the document and the
    # rows it renders. Only the role is decided here: this runs as fde_agent,
    # the gate runs the same text as fde_prodops.
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(WORKFLOW_SQL, {"wid": workflow_id})
            workflow = await fetchone(cur)
            if workflow is None:
                missing: dict[str, Any] = {
                    "error": "workflow not found",
                    "hint": "check workflow_id",
                }
                await emit_trace(conn, "wf_export_playbook", missing, latency_ms=int(now_ms() - t0))
                return missing

            await cur.execute(STEPS_SQL, {"wid": workflow_id})
            steps = await fetchall(cur)

            await cur.execute(
                PROCESS_FLOW_SQL,
                {"eng": workflow["engagement_id"], "key": workflow["root_process_key"]},
            )
            process_flow = await fetchall(cur)

        bindings = {str(step["step_key"]): step["bindings"] or [] for step in steps}
        result = {
            "workflow_id": workflow_id,
            "slug": workflow["slug"],
            "version": workflow["version"],
            "status": workflow["status"],
            "markdown": render_playbook(workflow, steps, bindings, process_flow),
        }
        await emit_trace(conn, "wf_export_playbook", result, latency_ms=int(now_ms() - t0))
        return result


@pg_error_boundary
async def wf_draft(
    engagement_id: str,
    slug: str,
    *,
    title: str,
    root_process_key: str,
    pinned_commit_id: int,
    steps: list[StepSpec],
) -> dict[str, Any]:
    """Author a DRAFT workflow (status='draft') with its steps and
    faithfulness bindings, pinned to `pinned_commit_id`. NEVER PUBLISHES --
    publishing is a separate, human-gated action this tool cannot perform
    (the agent role has no UPDATE grant on wf.workflow/wf.step, only
    INSERT, by design).

    After inserting everything, calls wf.assert_faithful and, if it fails
    (an unbound non-notify step, or a binding to a key not live as of
    `pinned_commit_id`), RE-RAISES ITS MESSAGE VERBATIM and ROLLS BACK THE
    ENTIRE DRAFT -- nothing is left half-created. Fix the reported step(s)
    (usually: add a StepBindingSpec, or bind to a different/newer key) and
    call this again. On success, returns the new workflow_id/workflow_uuid
    and version (auto-incremented per slug so re-drafting the same slug
    never collides).
    """
    t0 = now_ms()
    agent = get_settings().agent
    async with db.tool_transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT content_digest FROM kg.commit WHERE commit_id = %(cid)s",
                {"cid": pinned_commit_id},
            )
            commit_row = await fetchone(cur)
            pinned_digest = commit_row["content_digest"] if commit_row else None

            await cur.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM wf.workflow "
                "WHERE engagement_id = %(eng)s::uuid AND slug = %(slug)s",
                {"eng": engagement_id, "slug": slug},
            )
            next_version_row = await fetchone(cur)
            assert next_version_row is not None, "COALESCE(...) + 1 always yields a row"
            next_version = next_version_row["next_version"]

            await cur.execute(
                """
                INSERT INTO wf.workflow
                    (engagement_id, slug, version, title, pinned_commit_id,
                     pinned_digest, root_process_key, authored_by)
                VALUES (%(eng)s::uuid, %(slug)s, %(version)s, %(title)s, %(pinned)s,
                        %(digest)s, %(root)s, %(authored_by)s)
                RETURNING workflow_id, workflow_uuid, version, status
                """,
                {
                    "eng": engagement_id,
                    "slug": slug,
                    "version": next_version,
                    "title": title,
                    "pinned": pinned_commit_id,
                    "digest": pinned_digest,
                    "root": root_process_key,
                    "authored_by": agent.runtime_arn,
                },
            )
            workflow = await fetchone(cur)
            assert workflow is not None, "RETURNING always yields exactly one row here"
            workflow_id = workflow["workflow_id"]

            for step in steps:
                await cur.execute(
                    """
                    INSERT INTO wf.step
                        (workflow_id, step_key, ordinal, kind, title, instruction,
                         tool_name, tool_args, human_prompt, human_schema, branches,
                         sor_adapter_key, sor_write_op, requires_human, timeout_seconds,
                         on_failure)
                    VALUES (%(wid)s, %(step_key)s, %(ordinal)s, %(kind)s::wf.step_kind,
                            %(title)s, %(instruction)s, %(tool_name)s,
                            %(tool_args)s, %(human_prompt)s, %(human_schema)s,
                            %(branches)s, %(sor_adapter_key)s, %(sor_write_op)s,
                            %(requires_human)s, %(timeout_seconds)s, %(on_failure)s)
                    RETURNING step_id
                    """,
                    {
                        "wid": workflow_id,
                        "step_key": step.step_key,
                        "ordinal": step.ordinal,
                        "kind": step.kind,
                        "title": step.title,
                        "instruction": step.instruction,
                        "tool_name": step.tool_name,
                        "tool_args": Jsonb(step.tool_args) if step.tool_args is not None else None,
                        "human_prompt": step.human_prompt,
                        "human_schema": Jsonb(step.human_schema)
                        if step.human_schema is not None
                        else None,
                        "branches": Jsonb(step.branches) if step.branches is not None else None,
                        "sor_adapter_key": step.sor_adapter_key,
                        "sor_write_op": step.sor_write_op,
                        "requires_human": step.requires_human,
                        "timeout_seconds": step.timeout_seconds,
                        "on_failure": step.on_failure,
                    },
                )
                step_id_row = await fetchone(cur)
                assert step_id_row is not None, "RETURNING always yields exactly one row here"
                step_id = step_id_row["step_id"]

                for binding in step.bindings:
                    if binding.subject_kind == "node":
                        await cur.execute(
                            "SELECT label FROM kg.node_current WHERE engagement_id = %(eng)s::uuid AND node_key = %(key)s",
                            {"eng": engagement_id, "key": binding.subject_key},
                        )
                    else:
                        await cur.execute(
                            "SELECT label FROM kg.edge_current WHERE engagement_id = %(eng)s::uuid AND edge_key = %(key)s",
                            {"eng": engagement_id, "key": binding.subject_key},
                        )
                    label_row = await fetchone(cur)
                    pinned_label = label_row["label"] if label_row else None

                    await cur.execute(
                        """
                        INSERT INTO wf.step_binding (step_id, subject_kind, subject_key, relation, pinned_label)
                        VALUES (%(sid)s, %(kind)s, %(key)s, %(relation)s, %(label)s)
                        """,
                        {
                            "sid": step_id,
                            "kind": binding.subject_kind,
                            "key": binding.subject_key,
                            "relation": binding.relation,
                            "label": pinned_label,
                        },
                    )

            await cur.execute("SELECT wf.assert_faithful(%(wid)s)", {"wid": workflow_id})

        result = {
            "workflow_id": workflow_id,
            "workflow_uuid": str(workflow["workflow_uuid"]),
            "version": workflow["version"],
            "status": workflow["status"],
            "step_count": len(steps),
            "faithful": True,
            "note": "draft only -- publishing requires a separate human-gated action this tool cannot perform.",
        }
        await emit_trace(conn, "wf_draft", result, latency_ms=int(now_ms() - t0))
        return result


def register(mcp: FastMCP[None]) -> None:
    """Register every workflow tool on `mcp`."""
    mcp.tool()(wf_list)
    mcp.tool()(wf_get)
    mcp.tool()(wf_export_playbook)
    mcp.tool()(wf_draft)
