"""ui.py -- the prod-ops console: server-rendered HTML, in the same Lambda.

No SPA, no CORS, no S3 bucket, no build toolchain, no JavaScript. The console
calls the same `service/*` functions the JSON API does, in-process, and
renders the result with Jinja2. That is one deployable instead of three, and
one authorization story instead of two.

Every mutation is POST -> 303 -> GET. A reviewer who refreshes after
approving something must not record a second decision, and 303 is the status
that makes the browser follow with GET rather than leaving re-submission to
its own judgement.

`review_seconds` without JavaScript
------------------------------------
`hitl.gate_decision.review_seconds` is a training-quality signal: db/004:176
points out that a four-second approval on a thirty-item proposal is a rubber
stamp, not a label. It is measured by stamping the render time into a hidden
field and subtracting it on submit -- so the number is wall-clock time the
page was open, computed on the server from a value the server wrote. A
client-side timer would have needed JavaScript to produce a number the
server would have had to trust anyway.

Autoescaping is not optional here
----------------------------------
Proposal payloads, step instructions and drift details are all written by
agents, and a proposal payload containing markup is not a hypothetical --
it is a document extracted from a customer's SOP. The environment is built
with `autoescape=True` and every template renders untrusted values through
either autoescaping or `|tojson`, which escapes `<`, `>` and `&` inside the
JSON as well.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from fde_gate.http import GateError, Request, Response, pg_message
from fde_gate.runner import advance, default_invoker
from fde_gate.service import drift, proposals, runs, workflows

if TYPE_CHECKING:
    from pathlib import Path

    from fde_gate.http import Router

import psycopg

from fde_mcp.logging import get_logger

log = get_logger(__name__)

__all__ = ["register", "render"]


def _template_dir() -> Path:
    from pathlib import Path as _Path  # noqa: PLC0415 -- keeps the TYPE_CHECKING import honest

    return _Path(__file__).parent / "templates"


# StrictUndefined so a template referencing a key the service stopped
# returning fails loudly in tests instead of rendering an empty cell that
# nobody notices until an operator asks why a column is blank.
_ENV = Environment(
    loader=FileSystemLoader(str(_template_dir())),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(template: str, **context: Any) -> str:
    return _ENV.get_template(template).render(**context)


def _page(request: Request, template: str, **context: Any) -> Response:
    """Render a page with the chrome every template expects."""
    return Response.html(
        render(
            template,
            principal=request.principal,
            error=request.query.get("error"),
            notice=request.query.get("notice"),
            rendered_at=time.time(),
            **context,
        )
    )


def _back(location: str, *, error: str | None = None, notice: str | None = None) -> Response:
    """Redirect, carrying a message the destination page will display.

    The message travels in the query string rather than in a session or a
    flash cookie because this service holds no session state at all -- the
    JWT is the whole of it. The cost is an ugly URL after a failed action;
    the benefit is that there is nothing to expire, share, or forge.
    """
    if error:
        return Response.redirect(f"{location}?error={quote(error, safe='')}")
    if notice:
        return Response.redirect(f"{location}?notice={quote(notice, safe='')}")
    return Response.redirect(location)


def _message(exc: Exception) -> str:
    """The text a person should read, from whatever went wrong.

    Postgres RAISE messages reach the screen verbatim: "reviewer x holds no
    live control authority on engagement y (grant it in
    hitl.reviewer_authority...)" is an instruction, and summarising it to
    "permission denied" would throw away the only part that helps.
    """
    if isinstance(exc, GateError):
        return exc.message
    if isinstance(exc, psycopg.Error):
        return pg_message(exc)
    return str(exc)


def _review_seconds(request: Request) -> int | None:
    """Wall-clock seconds between the page render and this submit."""
    raw = request.form.get("rendered_at")
    if not raw:
        return None
    try:
        elapsed = time.time() - float(raw)
    except ValueError:
        return None
    return max(0, int(elapsed))


def _item_verdicts(request: Request) -> dict[str, Any]:
    """Collect per-item verdicts from the decision form.

    The form names each control `verdict_<item_id>` and `payload_<item_id>`,
    so the item ids come from the form rather than from a re-query -- an item
    that vanished between render and submit is then rejected by
    `hitl.record_decision` ("item N is not an item of proposal M") instead of
    being silently skipped.
    """
    verdicts: dict[str, Any] = {}
    for key, value in request.form.items():
        if not key.startswith("verdict_") or not value:
            continue
        item_id = key[len("verdict_") :]
        if value == "edit":
            raw = request.form.get(f"payload_{item_id}", "").strip()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError as exc:
                raise GateError(
                    400, f"item {item_id}: edited payload is not valid JSON ({exc})"
                ) from None
            if not isinstance(payload, dict):
                raise GateError(400, f"item {item_id}: edited payload must be a JSON object")
            verdicts[item_id] = {"verdict": "edit", "payload": payload}
        else:
            verdicts[item_id] = {"verdict": value}
    return verdicts


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


async def queue_page(request: Request) -> Response:
    """The operator's home: proposals to review and steps waiting on a human."""
    queue = await proposals.list_queue(request.principal, mine=request.query.get("mine") == "1")
    awaiting = await runs.awaiting_steps(request.principal)
    return _page(
        request,
        "queue.html.j2",
        proposals=queue["proposals"],
        awaiting_steps=awaiting["awaiting_steps"],
        mine=request.query.get("mine") == "1",
    )


async def proposal_page(request: Request) -> Response:
    proposal_id = request.param_int("proposal_id")
    proposal = await proposals.get_proposal(proposal_id, request.principal)
    if "error" in proposal:
        return _page(request, "not_found.html.j2", what=f"proposal {proposal_id}")
    return _page(request, "proposal.html.j2", proposal=proposal)


async def decision_post(request: Request) -> Response:
    proposal_id = request.param_int("proposal_id")
    location = f"/ui/proposals/{proposal_id}"
    try:
        gate_id = int(request.field_str("gate_id"))
        result = await proposals.record_decision(
            gate_id,
            request.principal,
            request.field_str("decision"),
            comment=request.form.get("comment") or None,
            item_verdicts=_item_verdicts(request),
            review_seconds=_review_seconds(request),
        )
    except Exception as exc:  # rendered to the reviewer, never swallowed
        log.info("ui_decision_refused", proposal_id=proposal_id, message=_message(exc))
        return _back(location, error=_message(exc))
    status = "unknown" if result["proposal"] is None else result["proposal"]["status"]
    return _back(location, notice=f"decision recorded; proposal is now {status}")


async def item_edit_post(request: Request) -> Response:
    item_id = request.param_int("item_id")
    proposal_id = request.form.get("proposal_id", "")
    location = f"/ui/proposals/{proposal_id}" if proposal_id else "/ui"
    raw = request.form.get("payload", "").strip()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise GateError(400, "payload must be a JSON object")
        await proposals.edit_item(item_id, request.principal, payload)
    except json.JSONDecodeError as exc:
        return _back(location, error=f"payload is not valid JSON ({exc})")
    except Exception as exc:  # rendered to the reviewer
        return _back(location, error=_message(exc))
    return _back(location, notice=f"item {item_id} edited")


async def merge_post(request: Request) -> Response:
    proposal_id = request.param_int("proposal_id")
    location = f"/ui/proposals/{proposal_id}"
    try:
        result = await proposals.merge(proposal_id, request.principal)
    except Exception as exc:  # rendered to the reviewer
        return _back(location, error=_message(exc))
    commit = result["commit"] or {}
    return _back(location, notice=f"merged as commit {commit.get('commit_id')}")


async def workflows_page(request: Request) -> Response:
    status = request.query.get("status", "published")
    listing = await workflows.list_workflows(status=None if status == "all" else status)
    return _page(request, "workflows.html.j2", workflows=listing["workflows"], status=status)


async def workflow_page(request: Request) -> Response:
    """One workflow as a procedure a person can read before publishing it.

    The page the publish button should always have stood on: publishing used
    to be a button on a list row, so the only way to see what was being
    published was to start a run of it.
    """
    workflow_id = request.param_int("workflow_id")
    playbook = await workflows.get_playbook(workflow_id)
    if "error" in playbook:
        return _page(request, "not_found.html.j2", what=f"workflow {workflow_id}")
    return _page(
        request,
        "workflow.html.j2",
        workflow=playbook["workflow"],
        steps=playbook["steps"],
        process_flow=playbook["process_flow"],
    )


async def publish_post(request: Request) -> Response:
    """Publish, then land on the workflow -- including when it is refused.

    `wf.assert_faithful` refuses by naming the unbound step keys, and the
    detail page is where those steps are, each already flagged as citing
    nothing. Redirecting to the list would put the instruction and the thing
    it is about on two different screens.
    """
    workflow_id = request.param_int("workflow_id")
    location = f"/ui/workflows/{workflow_id}"
    try:
        await workflows.publish(workflow_id, request.principal)
    except Exception as exc:  # rendered to the operator
        return _back(location, error=_message(exc))
    return _back(location, notice=f"workflow {workflow_id} published")


async def runs_page(request: Request) -> Response:
    status = request.query.get("status")
    listing = await runs.list_runs(statuses=(status,) if status else None)
    published = await workflows.list_workflows(status="published")
    return _page(
        request,
        "runs.html.j2",
        runs=listing["runs"],
        workflows=published["workflows"],
        status=status,
    )


async def run_page(request: Request) -> Response:
    run_id = request.param_int("run_id")
    run = await runs.get_run(run_id)
    if "error" in run:
        return _page(request, "not_found.html.j2", what=f"run {run_id}")
    return _page(request, "run.html.j2", run=run)


async def run_start_post(request: Request) -> Response:
    try:
        workflow_id = int(request.field_str("workflow_id"))
        raw = request.form.get("input", "").strip()
        run_input = json.loads(raw) if raw else {}
        if not isinstance(run_input, dict):
            raise GateError(400, "input must be a JSON object")
        started = await runs.start_run(
            workflow_id, request.principal, run_input=run_input, groups=request.groups
        )
    except json.JSONDecodeError as exc:
        return _back("/ui/runs", error=f"input is not valid JSON ({exc})")
    except Exception as exc:  # rendered to the operator
        return _back("/ui/runs", error=_message(exc))

    run_id = int(started["run"]["run_id"])
    try:
        await advance(run_id, invoker=await default_invoker())
    except Exception as exc:  # the run exists; say why it did not move
        return _back(f"/ui/runs/{run_id}", error=_message(exc))
    return _back(f"/ui/runs/{run_id}", notice=f"run {run_id} started")


async def respond_post(request: Request) -> Response:
    run_step_id = request.param_int("run_step_id")
    run_id = request.form.get("run_id", "")
    location = f"/ui/runs/{run_id}" if run_id else "/ui"
    try:
        raw = request.form.get("response", "").strip()
        response_body = json.loads(raw) if raw else {}
        if not isinstance(response_body, dict):
            raise GateError(400, "response must be a JSON object")
        answered = await runs.respond(
            run_step_id,
            request.principal,
            request.field_str("action"),
            response=response_body,
        )
        run = answered["run"]
        if run is not None and run["status"] in ("pending", "running"):
            await advance(int(run["run_id"]), invoker=await default_invoker())
    except json.JSONDecodeError as exc:
        return _back(location, error=f"response is not valid JSON ({exc})")
    except Exception as exc:  # rendered to the operator
        return _back(location, error=_message(exc))
    return _back(location, notice=f"step {run_step_id} answered")


async def cancel_post(request: Request) -> Response:
    run_id = request.param_int("run_id")
    try:
        await runs.cancel(run_id, request.principal)
    except Exception as exc:  # rendered to the operator
        return _back(f"/ui/runs/{run_id}", error=_message(exc))
    return _back(f"/ui/runs/{run_id}", notice="run cancelled")


async def drift_page(request: Request) -> Response:
    severity = request.query.get("severity")
    listing = await drift.list_signals(severity=severity)
    return _page(request, "drift.html.j2", signals=listing["signals"], severity=severity)


async def triage_post(request: Request) -> Response:
    signal_id = request.param_int("signal_id")
    try:
        await drift.triage(
            signal_id,
            request.principal,
            request.field_str("state"),
            note=request.form.get("note") or None,
        )
    except Exception as exc:  # rendered to the operator
        return _back("/ui/drift", error=_message(exc))
    return _back("/ui/drift", notice=f"signal {signal_id} triaged")


def register(router: Router) -> None:
    """Attach the console's 15 routes to the shared router."""
    router.get("/ui", queue_page)
    router.get("/ui/proposals/{proposal_id}", proposal_page)
    router.post("/ui/proposals/{proposal_id}/decision", decision_post)
    router.post("/ui/proposals/{proposal_id}/merge", merge_post)
    router.post("/ui/items/{item_id}/edit", item_edit_post)

    router.get("/ui/workflows", workflows_page)
    router.get("/ui/workflows/{workflow_id}", workflow_page)
    router.post("/ui/workflows/{workflow_id}/publish", publish_post)

    router.get("/ui/runs", runs_page)
    router.post("/ui/runs/start", run_start_post)
    router.get("/ui/runs/{run_id}", run_page)
    router.post("/ui/runs/{run_id}/cancel", cancel_post)
    router.post("/ui/run-steps/{run_step_id}/respond", respond_post)

    router.get("/ui/drift", drift_page)
    router.post("/ui/drift/{signal_id}/triage", triage_post)
