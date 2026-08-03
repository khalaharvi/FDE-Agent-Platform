"""Publishing a workflow, starting runs, answering and cancelling them."""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import pytest
from gate_seed import OWNER, SME
from psycopg import errors as pg_errors

from fde_gate import handler, ui
from fde_gate.config import get_gate_settings
from fde_gate.executors import StepExecutionError
from fde_gate.forms import fields_from_schema
from fde_gate.http import GateError, Request, parse_apigw_event
from fde_gate.runner import advance
from fde_gate.service import proposals, runs, workflows
from fde_mcp import db

pytestmark = pytest.mark.requires_db


async def test_publish_unfaithful_409_verbatim(make_workflow: Any) -> None:
    """A workflow with an unbound step cannot be published, and the refusal
    names the steps.

    `wf.assert_faithful` runs INSIDE `wf.publish_workflow`, so this holds
    even for a caller who never thought to check. The message lists the
    offending step keys and that list is the entire instruction for fixing
    it -- so it reaches the client unparaphrased.
    """
    workflow_id = make_workflow(
        [{"step_key": "unbound", "kind": "tool", "tool_name": "kg_get_node"}],
        publish=False,
        bind=False,
    )

    with pytest.raises(GateError) as excinfo:
        await workflows.publish(workflow_id, OWNER)

    assert excinfo.value.status == HTTPStatus.CONFLICT
    assert "unbound steps" in excinfo.value.message
    assert "unbound" in excinfo.value.message
    assert "every step must cite a" in excinfo.value.message

    # The refused publish left it in draft, not in some half state.
    listing = await workflows.list_workflows(status=None)
    row = next(w for w in listing["workflows"] if w["workflow_id"] == workflow_id)
    assert row["status"] == "draft"
    assert row["unbound_steps"] == 1


async def test_publishing_twice_is_a_conflict(make_workflow: Any) -> None:
    workflow_id = make_workflow(
        [{"step_key": "hold", "kind": "human", "human_prompt": "Proceed?"}], publish=True
    )
    with pytest.raises(GateError) as excinfo:
        await workflows.publish(workflow_id, OWNER)
    assert excinfo.value.status == HTTPStatus.CONFLICT
    assert "only a draft or in-review workflow can be published" in excinfo.value.message


async def test_publish_then_start_run_respects_runnable_by_groups(
    make_workflow: Any,
) -> None:
    """A draft will not run; a published one runs only for the right group."""
    workflow_id = make_workflow(
        [{"step_key": "hold", "kind": "human", "human_prompt": "Proceed?"}],
        publish=False,
        runnable_by=["prodops-team"],
    )

    # docs/10 §6, the first "why won't this start": it is not published.
    with pytest.raises(GateError) as excinfo:
        await runs.start_run(workflow_id, SME)
    assert excinfo.value.status == HTTPStatus.CONFLICT
    assert "not published" in excinfo.value.message
    assert "wf.publish_workflow" in excinfo.value.message

    published = await workflows.publish(workflow_id, OWNER)
    assert published["workflow"]["status"] == "published"
    assert published["workflow"]["published_by"] == OWNER
    # The digest is pinned alongside the commit id, so a tampered replay of
    # the pinned commit is detectable.
    assert published["workflow"]["pinned_digest"] is not None

    # docs/10 §6, the second: the operator is not in runnable_by.
    with pytest.raises(GateError) as denied:
        await runs.start_run(workflow_id, SME, groups=("some-other-team",))
    assert denied.value.status == HTTPStatus.CONFLICT
    assert "may not run workflow" in denied.value.message
    assert "prodops-team" in denied.value.message

    started = await runs.start_run(
        workflow_id, SME, run_input={"case": "abc"}, groups=("prodops-team",)
    )
    run = started["run"]
    assert run["status"] == "pending"
    assert run["started_by"] == SME
    # The input is seeded into the context, which is what makes
    # {"$ctx": "$.input.case"} work on the very first step.
    assert run["context"] == {"input": {"case": "abc"}}


async def test_respond_and_cancel(make_workflow: Any) -> None:
    """A human step waits, is answered, and its answer joins the context.

    Then a second run of the same workflow is cancelled while waiting, and
    the open attempt is cancelled with it rather than left running forever.
    """
    workflow_id = make_workflow(
        [
            {
                "step_key": "hold",
                "kind": "human",
                "human_prompt": "Proceed?",
                "human_schema": {"approved": "boolean"},
            }
        ]
    )

    run_id = int((await runs.start_run(workflow_id, SME))["run"]["run_id"])
    parked = await advance(run_id)
    assert parked["status"] == "awaiting_human"

    queue = await runs.awaiting_steps(SME)
    waiting = next(s for s in queue["awaiting_steps"] if s["run_id"] == run_id)
    assert waiting["human_prompt"] == "Proceed?"
    assert waiting["awaiting_principal"] is None, "unassigned means anyone on shift"
    assert waiting["escalated"] is False

    answered = await runs.respond(
        int(waiting["run_step_id"]), OWNER, "approve", response={"approved": True}
    )
    assert answered["run"]["status"] == "succeeded"

    detail = await runs.get_run(run_id)
    assert detail["context"]["hold"] == {"approved": True}
    (step,) = detail["steps"]
    assert step["status"] == "succeeded"
    assert step["responded_by"] == OWNER

    # Answering a step that is no longer waiting is a conflict, not a 500.
    with pytest.raises(GateError) as excinfo:
        await runs.respond(int(waiting["run_step_id"]), OWNER, "approve", response={})
    assert excinfo.value.status == HTTPStatus.CONFLICT
    assert "not awaiting a human" in excinfo.value.message

    # ---- cancel ----------------------------------------------------------
    second_id = int((await runs.start_run(workflow_id, SME))["run"]["run_id"])
    await advance(second_id)
    cancelled = await runs.cancel(second_id, OWNER)
    assert cancelled["run"]["status"] == "cancelled"
    assert cancelled["run"]["error"]["cancelled_by"] == OWNER

    after = await runs.get_run(second_id)
    assert [s["status"] for s in after["steps"]] == ["cancelled"]

    with pytest.raises(GateError) as already:
        await runs.cancel(second_id, OWNER)
    assert already.value.status == HTTPStatus.CONFLICT
    assert "already cancelled" in already.value.message


async def test_run_list_and_detail_carry_the_verbatim_error(make_workflow: Any) -> None:
    """docs/10 §2 tells the operator to read the error; it must be there."""

    class _AlwaysFails:
        async def execute(self, step: Any, run: Any) -> dict[str, Any]:
            raise StepExecutionError({"error": "CPQ returned 503", "attempted": "GET /quote"})

    workflow_id = make_workflow(
        [{"step_key": "fetch", "kind": "tool", "tool_name": "kg_get_node", "on_failure": "halt"}]
    )
    run_id = int((await runs.start_run(workflow_id, SME))["run"]["run_id"])
    await advance(run_id, executors={"tool": _AlwaysFails()})

    detail = await runs.get_run(run_id)
    assert detail["status"] == "failed"
    assert detail["error"]["error"] == "CPQ returned 503"
    (step,) = detail["steps"]
    assert step["error"] == {"error": "CPQ returned 503", "attempted": "GET /quote"}
    assert step["on_failure"] == "halt"

    listing = await runs.list_runs(statuses=("failed",))
    assert any(r["run_id"] == run_id for r in listing["runs"])


async def test_the_two_service_roles_hold_exactly_their_own_half() -> None:
    """The role split is a grant boundary, not a convention.

    `fde_gate_service` has schema USAGE on wf but no table privileges
    (db/010:47 grants USAGE; nothing grants SELECT), so publishing works only
    because `wf.publish_workflow` is SECURITY DEFINER -- and any workflow or
    run LIST/DETAIL query must run as `fde_prodops`. The reverse is just as
    load-bearing: product ops runs workflows and must not be able to publish
    one (db/010:64).

    Asserted rather than trusted, because moving one listing query onto the
    convenient role is a one-line change that would pass every other test in
    this suite right up until it reached an environment where the caller is
    not a superuser.
    """
    gate = get_gate_settings().gate.gate_role
    prodops = get_gate_settings().gate.prodops_role

    reads = (
        "SELECT 1 FROM wf.workflow LIMIT 1",
        "SELECT 1 FROM wf.run LIMIT 1",
        "SELECT 1 FROM wf.run_step LIMIT 1",
    )
    for statement in reads:
        with pytest.raises(pg_errors.InsufficientPrivilege):
            async with db.tool_transaction(role=gate) as conn:
                await conn.execute(statement)

    # ...and prodops may read all three.
    async with db.tool_transaction(role=prodops) as conn:
        for statement in reads:
            await conn.execute(statement)

    # Publishing is the gate service's alone.
    with pytest.raises(pg_errors.InsufficientPrivilege):
        async with db.tool_transaction(role=prodops) as conn:
            await conn.execute("SELECT wf.publish_workflow(1, 'x')")


async def test_gate_service_role_can_apply_trace_label(make_proposal: Any, sql: Any) -> None:
    """Regression for the grant gap db/013 closes (B17).

    db/011 gave `fde_gate_service` UPDATE on `trn.trace_session`'s three label
    columns, but db/010 only ever granted it schema USAGE on kg/hitl/wf/sor.
    Table privileges without schema USAGE are unreachable, so
    `hitl.apply_trace_label` failed at runtime with "permission denied for
    schema trn" -- meaning every merged proposal silently failed to label its
    trace, which is the one write the training pipeline cannot do without.

    This calls the label writer DIRECTLY as the gate role, after the proposal
    has reached a terminal human judgment so the function actually reaches
    its UPDATE rather than returning early.
    """
    session_id = str(uuid.uuid4())
    created = make_proposal(trace_session_id=session_id)
    gate_id = created["gates"][0]["gate_id"]

    await proposals.record_decision(gate_id, SME, "reject", comment="no")

    role = get_gate_settings().gate.gate_role
    async with db.tool_transaction(role=role) as conn:
        # The exact two statements the grant gap used to break.
        await conn.execute(
            "SELECT hitl.apply_trace_label(%(pid)s)", {"pid": created["proposal_id"]}
        )
        cur = await conn.execute(
            "SELECT outcome::text AS outcome, label_source FROM trn.trace_session "
            "WHERE session_id = %(s)s::uuid",
            {"s": session_id},
        )
        row = await cur.fetchone()

    assert row is not None
    assert row["outcome"] == "rejected"
    assert row["label_source"] == "hitl_gate"


async def test_a_run_that_exists_is_not_reported_as_missing(make_workflow: Any) -> None:
    """`wf.run` has an `error` column, so every real run carries an `error`
    key -- and both callers tested `"error" in result` to decide "not found".
    The console's run detail page and `GET /api/runs/{id}` therefore answered
    "no run N here" for every run that existed, from the day the column and
    the envelope first shared a name.

    Asserted on both routes, not just on `is_missing`, because the bug was
    never in the service -- it was in what two callers concluded from it.
    """
    workflow_id = make_workflow(
        [{"step_key": "approve", "kind": "human", "human_schema": {"approved": "boolean"}}]
    )
    # Deliberately started with NO input: the second bug below fires on every
    # run, and an input here would let a reader think it needed one.
    run_id = int((await runs.start_run(workflow_id, SME))["run"]["run_id"])
    # Advanced, so the run is PARKED on the human step rather than merely
    # started. `run.awaiting` being empty is what let this page look fine in
    # every earlier test while its one interesting branch never ran.
    await advance(run_id)

    detail = await runs.get_run(run_id)
    assert detail["status"] == "awaiting_human"
    assert len(detail["awaiting"]) == 1
    assert "error" in detail, "the column is still selected; that is not the bug"
    assert detail["error"] is None
    assert not runs.is_missing(detail)
    assert runs.is_missing(await runs.get_run(10**9))

    page = await ui.run_page(
        Request(
            method="GET",
            path=f"/ui/runs/{run_id}",
            path_params={"run_id": str(run_id)},
            principal=SME,
        )
    )
    assert page.status == HTTPStatus.OK
    assert "Not found" not in str(page.body)

    # The second bug the first one was hiding. `wf.begin_step` stores the run
    # context as the attempt's input (db/013:585-586) and `wf.start_run`
    # seeds that context as `{"input": ...}` (db/013:540) -- so `s.input` is
    # a non-empty object on every parked step, even for a run started with
    # no input at all, and `s.input.escalated_error` raised under
    # StrictUndefined rather than reading false.
    (parked,) = detail["awaiting"]
    assert parked["input"] == {"input": {}}, "non-empty despite an empty run input"
    assert "escalated_error" not in parked["input"]
    # Rendering the waiting section at all is the assertion.
    assert "Waiting on you" in str(page.body)
    assert "This step failed and was escalated" not in str(page.body)

    api = await handler.get_run(
        Request(
            method="GET",
            path=f"/api/runs/{run_id}",
            path_params={"run_id": str(run_id)},
            principal=SME,
        )
    )
    assert api.status == HTTPStatus.OK
    missing = await handler.get_run(
        Request(
            method="GET",
            path="/api/runs/999999999",
            path_params={"run_id": "999999999"},
            principal=SME,
        )
    )
    assert missing.status == HTTPStatus.NOT_FOUND


def _form_post(path: str, principal: str, pairs: list[tuple[str, str]]) -> Request:
    """A real urlencoded POST, parsed by the code API Gateway's events reach.

    A list of PAIRS, not a dict, and routed through `parse_apigw_event`,
    because the whole mechanism under test is what that parser does with a
    key that appears twice -- `dict(parse_qsl(...))`, last value wins. A test
    that handed `Request(form={...})` a Python dict could not express a
    repeated key at all, and so could not have caught this.
    """
    body = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)
    return parse_apigw_event(
        {
            "rawPath": path,
            "requestContext": {
                "http": {"method": "POST", "path": path},
                "authorizer": {"jwt": {"claims": {"sub": principal}}},
            },
            "headers": {"content-type": "application/x-www-form-urlencoded"},
            "body": body,
            "isBase64Encoded": False,
        }
    )


async def test_a_schema_field_named_workflow_id_cannot_redirect_the_run(
    make_workflow: Any,
) -> None:
    """End to end, through the real parser: a `human_schema` declaring
    `workflow_id` must not change WHICH workflow starts.

    The generated control renders after the hidden one, so before the prefix
    both were named `workflow_id`, the parser kept the last, and this POST
    started `decoy` -- the workflow the operator did not pick -- silently and
    with a 303 that looked like success. `wf_draft` accepts any object as a
    `human_schema`, so "workflow_id" reaching one needs no hostile author,
    only an ordinary name for a field.
    """
    chosen = make_workflow(
        [
            {
                "step_key": "approve",
                "kind": "human",
                "human_schema": {"workflow_id": "string", "note": "string"},
            }
        ]
    )
    decoy = make_workflow([{"step_key": "approve", "kind": "human"}])
    assert chosen != decoy

    # Exactly what the browser sends: the form's own hidden control first,
    # then each generated control under the name the macro gave it.
    fields = fields_from_schema({"workflow_id": "string", "note": "string"})
    generated = {f.name: f.input_name for f in fields}
    response = await ui.run_start_post(
        _form_post(
            "/ui/runs/start",
            SME,
            [
                ("workflow_id", str(chosen)),
                (generated["workflow_id"], str(decoy)),
                (generated["note"], "kept"),
            ],
        )
    )
    assert response.status == HTTPStatus.SEE_OTHER
    run_id = int(response.headers["Location"].removeprefix("/ui/runs/").split("?")[0])

    started = await runs.get_run(run_id)
    assert started["workflow_id"] == chosen, "the operator's choice, not the schema's"
    # ...and the schema's field still arrived, under its own name.
    assert started["input"] == {"workflow_id": str(decoy), "note": "kept"}
