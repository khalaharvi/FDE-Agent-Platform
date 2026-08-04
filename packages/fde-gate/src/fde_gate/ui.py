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
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from fde_gate.forms import fields_from_schema, values_from_form
from fde_gate.http import GateError, Request, Response, pg_message
from fde_gate.runner import advance, default_invoker
from fde_gate.service import (
    agents,
    dashboard,
    drift,
    is_missing,
    proposals,
    reviewers,
    runs,
    sources,
    workflows,
)

if TYPE_CHECKING:
    from fde_gate.http import Router

import psycopg

from fde_mcp.logging import get_logger

log = get_logger(__name__)

__all__ = ["is_console_path", "register", "render", "request_refused"]

#: Everything the console serves. Kept here, next to `register`, because this
#: module is the one that decides which paths are pages -- `handler.py` asks
#: rather than re-deriving the prefix, so adding a console route cannot leave
#: a second copy of the rule behind.
_CONSOLE_PREFIX = "/ui"


def is_console_path(path: str) -> bool:
    """Is this path one of the console's pages rather than the JSON API?

    Exact-or-child, not `startswith("/ui")`: a hypothetical "/uipsum" is not
    a page, and answering it with page chrome would be a claim about a route
    that does not exist.
    """
    return path == _CONSOLE_PREFIX or path.startswith(f"{_CONSOLE_PREFIX}/")


def _template_dir() -> Path:
    return Path(__file__).parent / "templates"


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


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """Agree with `count`. Registered as the `plural` Jinja filter.

    The intake pages count things an operator is being asked to trust --
    passages, dark passages, chunks -- and "1 chunks" undermines a number
    somebody is deciding whether to believe. Takes an explicit plural for the
    irregular cases the pages actually use ("is"/"are").
    """
    return singular if count == 1 else (plural if plural is not None else f"{singular}s")


def _day(timestamp: str) -> str:
    """The date half of an ISO timestamp. Registered as the `day` filter.

    Used for `kg.source.captured_at` and nowhere else. Every other timestamp
    this console renders is an EVENT -- when a gate was cleared, when a run
    started -- where the time of day is the useful part. `captured_at` is a
    date an operator typed into a date field, and echoing it back as
    "2026-07-29T00:00:00-05:00" answers a question they did not ask with a
    precision the value does not have.
    """
    return timestamp[:10] if len(timestamp) >= 10 else timestamp


def _error_text(error: Any) -> str:
    """The sentence inside a stored error envelope. The `error_text` filter.

    `wf.agent_launch.error` holds `StepExecutionError.detail` verbatim, which
    is a JSON object whose `error` key is the message and whose other keys are
    context. An operator reading the launch list wants the message; rendering
    the whole object gives them the message wrapped in braces. Anything that
    is not that shape is shown as JSON rather than guessed at -- the same
    trade `_message` makes, and the same one `launch` makes when it turns a
    detail into a GateError.
    """
    if isinstance(error, dict):
        message = error.get("error")
        if isinstance(message, str):
            return message
    return json.dumps(error)


_ENV.filters["plural"] = _plural
_ENV.filters["day"] = _day
_ENV.filters["error_text"] = _error_text


def render(template: str, **context: Any) -> str:
    return _ENV.get_template(template).render(**context)


async def _page(
    request: Request,
    template: str,
    *,
    error: str | None = None,
    http_status: int = 200,
    **context: Any,
) -> Response:
    """Render a page with the chrome every template expects.

    Async, and one query heavier than it looks, because `base.html.j2` shows
    the Reviewers link only to administrators and every page extends it. The
    alternative -- computing `is_admin` in the handlers that happen to need
    it and defaulting it false elsewhere -- means an administrator can only
    find the page from the page, which is not a link.

    Nothing is authorised here. The flag decides what is DISPLAYED; every
    reviewers.* function re-checks inside its own transaction, so a stale
    link is a 403, not an action.

    `error` is for the one case the POST/redirect/GET rule cannot serve: a
    form whose contents are too expensive to lose. Redirecting a rejected
    New source submission would discard the transcript someone just pasted,
    so that handler re-renders the form with the message instead, and the
    message has to arrive by argument rather than through the query string.

    `http_status` is spelled that way, and not `status`, because two pages
    already pass a DOMAIN `status` through `**context` -- the workflow and
    run filters. A parameter named `status` would silently swallow those
    into the response code instead of the template.
    """
    return Response.html(
        render(
            template,
            principal=request.principal,
            is_admin=await reviewers.is_admin(request.principal),
            error=error or request.query.get("error"),
            notice=request.query.get("notice"),
            rendered_at=time.time(),
            **context,
        ),
        status=http_status,
    )


async def _forbidden(request: Request, exc: GateError) -> Response:
    """A 403 an operator can read, for the console's HTML routes.

    The router's default is the JSON error envelope, which is right for the
    API and wrong for a browser: it renders as a wall of braces in place of
    the page. Both GET and POST land here, so a form submitted from a page
    that went stale answers with the same 403 as a direct request.
    """
    return Response.html(
        render(
            "not_authorized.html.j2",
            principal=request.principal,
            is_admin=False,
            error=None,
            notice=None,
            rendered_at=time.time(),
            reason=exc.message,
        ),
        status=exc.status,
    )


def request_refused(exc: GateError, *, principal: str = "") -> Response:
    """A refusal raised BEFORE routing, rendered as a page for a console path.

    The sibling of `_forbidden`, and it exists for the reason that one's
    docstring gives: the JSON envelope is right for the API and renders as a
    wall of braces in place of the page for a browser. The difference is when
    it is reachable -- `parse_apigw_event` runs before any route is matched,
    so there is no `Request` yet, and the refusals it raises (a PDF in the
    upload box, a mismatched multipart boundary) used to reach the operator
    as that wall.

    Nothing here touches the database, so `is_admin` is false rather than
    looked up: the nav loses one link on an error page, which is a better
    trade than a query on the path where the request could not even be read.
    """
    return Response.html(
        render(
            "request_refused.html.j2",
            principal=principal,
            is_admin=False,
            error=None,
            notice=None,
            rendered_at=time.time(),
            reason=exc.message,
        ),
        status=exc.status,
    )


def _back(location: str, *, error: str | None = None, notice: str | None = None) -> Response:
    """Redirect, carrying a message the destination page will display.

    The message travels in the query string rather than in a session or a
    flash cookie because this service holds no session state at all -- the
    JWT is the whole of it. The cost is an ugly URL after a failed action;
    the benefit is that there is nothing to expire, share, or forge.

    `location` may already carry a query string -- "/ui/runs?workflow_id=3"
    is how a refused run start gets the operator back to the form they were
    filling in rather than to an empty one.
    """
    separator = "&" if "?" in location else "?"
    if error:
        return Response.redirect(f"{location}{separator}error={quote(error, safe='')}")
    if notice:
        return Response.redirect(f"{location}{separator}notice={quote(notice, safe='')}")
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
    return await _page(
        request,
        "queue.html.j2",
        proposals=queue["proposals"],
        awaiting_steps=awaiting["awaiting_steps"],
        mine=request.query.get("mine") == "1",
    )


async def proposal_page(request: Request) -> Response:
    proposal_id = request.param_int("proposal_id")
    proposal = await proposals.get_proposal(proposal_id, request.principal)
    if is_missing(proposal, "proposal_id"):
        return await _page(request, "not_found.html.j2", what=f"proposal {proposal_id}")
    return await _page(request, "proposal.html.j2", proposal=proposal)


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
    return await _page(request, "workflows.html.j2", workflows=listing["workflows"], status=status)


async def workflow_page(request: Request) -> Response:
    """One workflow as a procedure a person can read before publishing it.

    The page the publish button should always have stood on: publishing used
    to be a button on a list row, so the only way to see what was being
    published was to start a run of it.
    """
    workflow_id = request.param_int("workflow_id")
    playbook = await workflows.get_playbook(workflow_id)
    if is_missing(playbook, "workflow"):
        return await _page(request, "not_found.html.j2", what=f"workflow {workflow_id}")
    return await _page(
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
    """The run list, the form that starts a new one, and the launch records.

    `?workflow_id=` is the second step: with one chosen, the page renders the
    fields that workflow's declared answer shape produces instead of the JSON
    textarea it used to demand. Without one, only the picker -- there is no
    way to know which fields to show before knowing which workflow.

    Agent launches are listed here rather than on a page of their own. They
    are not runs -- a launch has no workflow, no steps and no pinned commit,
    and the section says so -- but they are the same question asked of a
    different verb: what did I set going, and how did it end. A separate route
    would be a fourth nav entry an operator has to know exists BEFORE the
    launch they are trying to chase down, which is precisely when they do not.

    The two lists come from two different DB roles: runs through
    `fde_prodops`, launches through the gate role, because db/018 grants
    `wf.agent_launch` to the gate service alone. Two transactions, both
    read-only, on a page that already opens one for the nav's admin flag.

    `?status=` filters the runs and deliberately not the launches. The values
    it takes are `wf.run_status` members -- `awaiting_human`, `cancelled` --
    and a launch can be none of them; quietly emptying the launch table
    because a run filter was applied to it would read as "no launches".
    """
    status = request.query.get("status")
    listing = await runs.list_runs(statuses=(status,) if status else None)
    launched = await agents.list_launches()
    published = await workflows.list_workflows(status="published")

    chosen = request.query.get("workflow_id") or ""
    selected = next(
        (w for w in published["workflows"] if str(w["workflow_id"]) == chosen),
        None,
    )
    declared = None if selected is None else await workflows.input_schema(int(chosen))
    return await _page(
        request,
        "runs.html.j2",
        runs=listing["runs"],
        launches=launched["launches"],
        workflows=published["workflows"],
        status=status,
        selected_workflow=selected,
        input_schema_step=declared,
        input_fields=[] if declared is None else fields_from_schema(declared["schema"]),
    )


async def run_page(request: Request) -> Response:
    run_id = request.param_int("run_id")
    run = await runs.get_run(run_id)
    if runs.is_missing(run):
        return await _page(request, "not_found.html.j2", what=f"run {run_id}")
    return await _page(
        request,
        "run.html.j2",
        run=run,
        # Per awaiting step, because two steps of one run can be parked at
        # once and each declares its own answer shape. Built from the rows
        # `get_run` already read -- no second query.
        answer_fields={
            int(step["run_step_id"]): fields_from_schema(step.get("human_schema"))
            for step in run["awaiting"]
        },
    )


async def _run_input(request: Request, workflow_id: int) -> dict[str, Any]:
    """The new run's input, from generated fields or the JSON fallback.

    Which one is decided by re-reading the workflow's declared shape, not by
    looking at what the form happened to contain: a browser posting `input`
    to a workflow that declares fields would otherwise choose its own parser.
    """
    declared = await workflows.input_schema(workflow_id)
    fields = [] if declared is None else fields_from_schema(declared["schema"])
    if fields:
        return values_from_form(fields, request.form)
    raw = request.form.get("input", "").strip()
    run_input = json.loads(raw) if raw else {}
    if not isinstance(run_input, dict):
        raise GateError(HTTPStatus.BAD_REQUEST, "input must be a JSON object")
    return run_input


async def run_start_post(request: Request) -> Response:
    workflow_id = 0
    try:
        workflow_id = int(request.field_str("workflow_id"))
        started = await runs.start_run(
            workflow_id,
            request.principal,
            run_input=await _run_input(request, workflow_id),
            groups=request.groups,
        )
    except json.JSONDecodeError as exc:
        return _back(_start_form(workflow_id), error=f"input is not valid JSON ({exc})")
    except Exception as exc:  # rendered to the operator
        return _back(_start_form(workflow_id), error=_message(exc))

    run_id = int(started["run"]["run_id"])
    try:
        await advance(run_id, invoker=await default_invoker())
    except Exception as exc:  # the run exists; say why it did not move
        return _back(f"/ui/runs/{run_id}", error=_message(exc))
    return _back(f"/ui/runs/{run_id}", notice=f"run {run_id} started")


def _start_form(workflow_id: int) -> str:
    """Back to the start form with the same workflow still chosen.

    A refused run start used to land on an empty form; whatever had been
    filled in was gone, including the reason it was refused being about
    fields that were no longer on screen.
    """
    return "/ui/runs" if workflow_id <= 0 else f"/ui/runs?workflow_id={workflow_id}"


async def _answer_body(request: Request, run_step_id: int) -> dict[str, Any]:
    """The human response, from generated fields or the JSON fallback.

    The schema is re-read from the step rather than trusted from the form --
    see `runs.step_schema`.
    """
    fields = fields_from_schema(await runs.step_schema(run_step_id))
    if fields:
        return values_from_form(fields, request.form)
    raw = request.form.get("response", "").strip()
    response_body = json.loads(raw) if raw else {}
    if not isinstance(response_body, dict):
        raise GateError(HTTPStatus.BAD_REQUEST, "response must be a JSON object")
    return response_body


async def respond_post(request: Request) -> Response:
    run_step_id = request.param_int("run_step_id")
    run_id = request.form.get("run_id", "")
    location = f"/ui/runs/{run_id}" if run_id else "/ui"
    try:
        response_body = await _answer_body(request, run_step_id)
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
    return await _page(request, "drift.html.j2", signals=listing["signals"], severity=severity)


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


# ---------------------------------------------------------------------------
# The decision dashboard
# ---------------------------------------------------------------------------


def _dashboard_scope(request: Request) -> tuple[str | None, int]:
    """`?engagement_id=` and `?window_days=`, both optional.

    An unparseable or unoffered `window_days` falls back to 30 rather than
    refusing: this is a read-only view reached from a link, and a stale
    bookmark carrying `window_days=45` should show a dashboard, not an error
    page. The engagement is passed to Postgres as a uuid and a malformed one
    is its problem to reject, the way every other filter on this console
    works.
    """
    raw = request.query.get("window_days")
    try:
        window = int(raw) if raw else 30
    except ValueError:
        window = 30
    if window not in dashboard.WINDOW_CHOICES:
        window = 30
    return (request.query.get("engagement_id") or None, window)


async def dashboard_page(request: Request) -> Response:
    """The aggregate view: how decisions are flowing, for a whole engagement.

    Every other console page answers "what is this one thing"; this one
    answers "how are we doing", which is the question an operator opens the
    console with and previously had to reconstruct by counting rows on five
    other pages.

    The HTML comes from `fde_mcp.dashboard` pre-rendered and is dropped into
    the template with `|safe`. See the template for why that is sound here
    and nowhere else.
    """
    engagement_id, window_days = _dashboard_scope(request)
    built = await dashboard.build(engagement_id=engagement_id, window_days=window_days)
    query = urlencode(
        {k: v for k, v in (("engagement_id", engagement_id), ("window_days", window_days)) if v}
    )
    return await _page(
        request,
        "dashboard.html.j2",
        dashboard_html=built["html"],
        engagements=built["engagements"],
        engagement_id=engagement_id,
        window_days=window_days,
        window_choices=dashboard.WINDOW_CHOICES,
        md_href=f"/api/dashboard.md?{query}" if query else "/api/dashboard.md",
    )


# ---------------------------------------------------------------------------
# Evidence intake
#
# The one part of the console where losing the form contents is unacceptable:
# the field is a transcript somebody spent an hour producing. So every
# failure on the way in re-renders the page with what was typed still in it,
# and only the actions that have already succeeded redirect.
# ---------------------------------------------------------------------------

_ALLOWED_UPLOAD_SUFFIXES = (".txt", ".md", ".markdown", ".text")


def _source_form(request: Request) -> dict[str, str]:
    """The New source form's fields, echoed back on every re-render."""
    return {
        "engagement_id": request.form.get("engagement_id", ""),
        "title": request.form.get("title", ""),
        "source_kind": request.form.get("source_kind", "interview"),
        "captured_at": request.form.get("captured_at", "")
        or datetime.now(tz=UTC).date().isoformat(),
        "text": request.form.get("text", ""),
    }


def _submitted_text(request: Request) -> tuple[str, str | None]:
    """The transcript and the filename it came from, from paste or upload.

    Refuses to choose when given both. Silently preferring one would mean an
    operator who pasted a transcript, then attached the wrong file, stores
    the wrong document under the right title -- and nothing on any later
    screen would reveal which one was kept.
    """
    pasted = request.form.get("text", "").strip()
    upload = request.files.get("file")
    if upload is None:
        return pasted, None

    suffix = Path(upload.filename).suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            f"{upload.filename!r} is not a text file. Upload a "
            f"{' or '.join(_ALLOWED_UPLOAD_SUFFIXES)} file, or paste the text "
            "into the box instead.",
        )
    if pasted:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            f"you pasted text AND attached {upload.filename!r}. Use one or the "
            "other so it is clear which document is being stored.",
        )
    return upload.content.strip(), upload.filename


async def sources_page(request: Request) -> Response:
    """Every registered source, with how much of each one search can reach."""
    engagement_id = request.query.get("engagement_id") or None
    try:
        listing = await sources.list_sources(request.principal, engagement_id=engagement_id)
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        raise
    return await _page(
        request,
        "sources.html.j2",
        sources=listing["sources"],
        engagements=listing["engagements"],
        engagement_id=engagement_id,
    )


async def source_new_page(request: Request) -> Response:
    try:
        listing = await sources.list_sources(request.principal)
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        raise
    return await _page(
        request,
        "source_new.html.j2",
        form=_source_form(request),
        engagements=listing["engagements"],
        source_kinds=listing["source_kinds"],
    )


async def _reshow_new(request: Request, message: str) -> Response:
    """Re-render the New source form carrying the message and the input back.

    A form validation failure that arrives before any database call -- an
    upload with the wrong extension, say -- reaches here without the
    reviewer check having run, so the listing it needs for the pickers can
    still refuse. That refusal wins: someone who may not register evidence
    should be told so, not handed the form again.
    """
    try:
        listing = await sources.list_sources(request.principal)
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        raise
    return await _page(
        request,
        "source_new.html.j2",
        error=message,
        http_status=HTTPStatus.BAD_REQUEST,
        form=_source_form(request),
        engagements=listing["engagements"],
        source_kinds=listing["source_kinds"],
    )


async def source_preview_post(request: Request) -> Response:
    """Chunk and anchor the submitted text, and show it before it is stored."""
    try:
        text, filename = _submitted_text(request)
        preview = await sources.preview(
            request.principal,
            engagement_id=request.field_str("engagement_id"),
            title=request.field_str("title"),
            source_kind=request.field_str("source_kind"),
            captured_at=request.field_str("captured_at"),
            text=text,
        )
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        return await _reshow_new(request, exc.message)
    except ValueError as exc:  # the chunker's own size refusal
        return await _reshow_new(request, str(exc))
    except Exception as exc:  # rendered to the operator, never swallowed
        return await _reshow_new(request, _message(exc))

    return await _page(
        request,
        "source_preview.html.j2",
        preview=preview,
        coverage=preview["coverage"],
        preview_text=text,
        filename=filename,
        dark_consequence=sources.DARK_CONSEQUENCE,
        dark_next_step=sources.DARK_NEXT_STEP,
    )


async def source_create_post(request: Request) -> Response:
    """Store the source and its chunks, then land on the source itself."""
    try:
        text, filename = _submitted_text(request)
        result = await sources.register(
            request.principal,
            engagement_id=request.field_str("engagement_id"),
            title=request.field_str("title"),
            source_kind=request.field_str("source_kind"),
            captured_at=request.field_str("captured_at"),
            text=text,
            filename=filename or request.form.get("filename") or None,
        )
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        return await _reshow_new(request, exc.message)
    except ValueError as exc:
        return await _reshow_new(request, str(exc))
    except Exception as exc:  # rendered to the operator
        return await _reshow_new(request, _message(exc))

    location = f"/ui/sources/{result['source_id']}"
    if not result["created"]:
        return _back(
            location,
            notice="this exact text was already registered; showing the source that holds it",
        )
    coverage = result["coverage"]
    return _back(
        location,
        notice=(
            f"stored {result['chunks']} {_plural(result['chunks'], 'passage')}; "
            f"{coverage.anchored} of them ({coverage.percent}%) "
            f"{_plural(coverage.anchored, 'is', 'are')} searchable now"
        ),
    )


async def source_page(request: Request) -> Response:
    source_id = request.param_int("source_id")
    try:
        detail = await sources.get_source(request.principal, source_id)
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        raise
    if is_missing(detail, "source"):
        return await _page(request, "not_found.html.j2", what=f"source {source_id}")
    return await _page(
        request,
        "source.html.j2",
        source=detail["source"],
        chunk_rows=detail["chunk_rows"],
        coverage=detail["coverage"],
        superseded_by=detail["superseded_by"],
        can_reingest=detail["can_reingest"],
        dark_consequence=sources.DARK_CONSEQUENCE,
        dark_next_step=sources.DARK_NEXT_STEP,
    )


async def source_reingest_post(request: Request) -> Response:
    """Carry this source's dark passages into a new version, re-anchored."""
    source_id = request.param_int("source_id")
    location = f"/ui/sources/{source_id}"
    try:
        result = await sources.reingest_dark_chunks(request.principal, source_id)
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        return _back(location, error=exc.message)
    except Exception as exc:  # rendered to the operator
        return _back(location, error=_message(exc))

    if not result["created"]:
        return _back(
            f"/ui/sources/{result['source_id']}",
            notice="nothing has changed since the last re-ingest; showing that version",
        )
    coverage = result["coverage"]
    return _back(
        f"/ui/sources/{result['source_id']}",
        notice=(
            f"re-ingested {result['chunks']} {_plural(result['chunks'], 'passage')} "
            f"as source {result['source_id']}; {coverage.anchored} "
            f"{_plural(coverage.anchored, 'is', 'are')} searchable now"
        ),
    )


# ---------------------------------------------------------------------------
# Agent task launcher
#
# The other end of the intake loop above: evidence goes in on /ui/sources,
# and this is where an operator asks an agent to read it. Same rule about
# losing form contents -- the paste box here holds a transcript too -- so
# every refusal re-renders the page with what was typed still in it.
# ---------------------------------------------------------------------------


def _agent_choice(request: Request) -> tuple[str | None, str | None, str | None]:
    """(engagement, agent, task) from wherever this request carries them.

    Query string on the GET that chooses them, form fields on the POST that
    acts, and one reader for both so the page a failed launch re-renders is
    the page it was launched from.
    """

    def pick(name: str) -> str | None:
        return (request.form.get(name) or request.query.get(name) or "").strip() or None

    return pick("engagement_id"), pick("agent"), pick("task")


async def _launcher_page(
    request: Request, *, error: str | None = None, http_status: int = 200
) -> Response:
    engagement_id, agent, task = _agent_choice(request)
    try:
        context = await agents.launcher_context(
            request.principal, engagement_id=engagement_id, agent=agent, task=task
        )
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        # An agent/task pair that does not exist -- a stale bookmark, or the
        # cross-product a single flat task dropdown makes reachable. Show the
        # picker again with the sentence naming what the pair should be.
        context = await agents.launcher_context(request.principal, engagement_id=engagement_id)
        error, http_status = exc.message, exc.status

    return await _page(
        request,
        "agent_run.html.j2",
        error=error,
        http_status=http_status,
        **{
            **context,
            # What was typed, back on the form. A transcript pasted into the
            # box is the one thing on this page nobody should have to produce
            # twice because the runtime was misconfigured.
            #
            # Read under `input_name`, which is what the control was named on
            # the way out -- reading `field.name` here found nothing and
            # silently echoed the empty default back, which is the failure
            # this whole branch exists to prevent.
            "fields": [
                field.with_value(request.form.get(field.input_name, field.value))
                for field in context["fields"]
            ],
        },
    )


async def agent_run_page(request: Request) -> Response:
    """Pick an engagement, an agent and a task; then fill in that task's fields."""
    return await _launcher_page(request)


async def agent_run_post(request: Request) -> Response:
    """Dispatch the chosen task, then land on the review queue.

    The queue, and not `/ui/runs`, because that is where the OUTCOME is: a
    launch produces a proposal, and a proposal is what somebody has to do
    something about. `/ui/runs` now carries the launch RECORD, which is the
    answer to a different question -- "did the thing I started actually
    run?" -- and the one an operator asks only when this redirect never
    arrived. So the notice names the record by number instead of sending
    them to it; a successful launch has nothing to chase.
    """
    try:
        result = await agents.launch(
            request.principal,
            agent=request.field_str("agent"),
            task=request.field_str("task"),
            engagement_id=request.field_str("engagement_id"),
            form=request.form,
        )
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        return await _launcher_page(request, error=exc.message, http_status=exc.status)
    except Exception as exc:  # rendered to the operator, never swallowed
        log.exception("ui_agent_launch_crashed", agent=request.form.get("agent"))
        return await _launcher_page(
            request, error=_message(exc), http_status=HTTPStatus.INTERNAL_SERVER_ERROR
        )

    return _back(
        "/ui",
        notice=(
            f"the {result['agent']} agent finished {result['task']} "
            f"({result['events']} {_plural(result['events'], 'event')}), "
            f"recorded as launch #{result['launch_id']} on Runs. "
            "Anything it proposed is in the queue below."
        ),
    )


async def reviewers_page(request: Request) -> Response:
    """The roster. Administrators only, checked in the service layer.

    The guard is `reviewers.list_roster` refusing, not a branch here: the
    same refusal then covers every POST below and anything else that ever
    reads the roster, rather than being re-implemented per route.
    """
    try:
        roster = await reviewers.list_roster(request.principal)
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        raise
    return await _page(
        request,
        "reviewers.html.j2",
        reviewers=roster["reviewers"],
        gate_kinds=roster["gate_kinds"],
        engagements=roster["engagements"],
    )


async def reviewer_add_post(request: Request) -> Response:
    try:
        added = await reviewers.add_reviewer(
            request.principal,
            principal=request.field_str("principal"),
            display_name=request.field_str("display_name"),
            email=request.form.get("email") or None,
        )
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        return _back("/ui/reviewers", error=_message(exc))
    except Exception as exc:  # rendered to the operator
        return _back("/ui/reviewers", error=_message(exc))
    reviewer = added["reviewer"] or {}
    return _back("/ui/reviewers", notice=f"added {reviewer.get('principal')}")


async def reviewer_active_post(request: Request) -> Response:
    reviewer_id = request.param_int("reviewer_id")
    active = request.field_str("active") == "1"
    try:
        await reviewers.set_active(request.principal, reviewer_id, active=active)
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        return _back("/ui/reviewers", error=_message(exc))
    except Exception as exc:  # rendered to the operator
        return _back("/ui/reviewers", error=_message(exc))
    return _back(
        "/ui/reviewers",
        notice=f"reviewer {reviewer_id} {'reactivated' if active else 'deactivated'}",
    )


async def reviewer_authority_post(request: Request) -> Response:
    """Grant or revoke one gate authority. One route, because the form that
    revokes and the form that grants differ only in a hidden field, and two
    routes would be two places to forget the admin check."""
    reviewer_id = request.param_int("reviewer_id")
    verb = "changed"
    try:
        action = request.field_str("action")
        engagement_id = request.field_str("engagement_id")
        gate_kind = request.field_str("gate_kind")
        # Spelled out rather than f"{action}ed", which produced "revokeed".
        verb = "granted" if action == "grant" else "revoked"
        if action == "grant":
            await reviewers.grant_authority(
                request.principal,
                reviewer_id,
                engagement_id=engagement_id,
                gate_kind=gate_kind,
            )
        elif action == "revoke":
            await reviewers.revoke_authority(
                request.principal,
                reviewer_id,
                engagement_id=engagement_id,
                gate_kind=gate_kind,
            )
        else:
            raise GateError(HTTPStatus.BAD_REQUEST, f"unknown action {action!r}")
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        return _back("/ui/reviewers", error=_message(exc))
    except Exception as exc:  # rendered to the operator
        return _back("/ui/reviewers", error=_message(exc))
    return _back("/ui/reviewers", notice=f"{verb} {gate_kind} for reviewer {reviewer_id}")


async def reviewer_admin_post(request: Request) -> Response:
    reviewer_id = request.param_int("reviewer_id")
    admin = request.field_str("admin") == "1"
    try:
        await reviewers.set_admin(request.principal, reviewer_id, admin=admin)
    except GateError as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return await _forbidden(request, exc)
        return _back("/ui/reviewers", error=_message(exc))
    except Exception as exc:  # rendered to the operator
        return _back("/ui/reviewers", error=_message(exc))
    return _back(
        "/ui/reviewers",
        notice=f"admin {'granted to' if admin else 'revoked from'} reviewer {reviewer_id}",
    )


def register(router: Router) -> None:
    """Attach the console's 29 routes to the shared router."""
    router.get("/ui", queue_page)
    router.get("/ui/proposals/{proposal_id}", proposal_page)
    router.post("/ui/proposals/{proposal_id}/decision", decision_post)
    router.post("/ui/proposals/{proposal_id}/merge", merge_post)
    router.post("/ui/items/{item_id}/edit", item_edit_post)

    # "/ui/sources/new" before "/ui/sources/{source_id}": routes match in
    # registration order, so the literal has to be offered first or "new"
    # arrives at the detail page as a source_id and 400s.
    router.get("/ui/sources", sources_page)
    router.get("/ui/sources/new", source_new_page)
    router.post("/ui/sources/preview", source_preview_post)
    router.post("/ui/sources", source_create_post)
    router.get("/ui/sources/{source_id}", source_page)
    router.post("/ui/sources/{source_id}/reingest", source_reingest_post)

    router.get("/ui/agents/run", agent_run_page)
    router.post("/ui/agents/run", agent_run_post)

    router.get("/ui/workflows", workflows_page)
    router.get("/ui/workflows/{workflow_id}", workflow_page)
    router.post("/ui/workflows/{workflow_id}/publish", publish_post)

    router.get("/ui/runs", runs_page)
    router.post("/ui/runs/start", run_start_post)
    router.get("/ui/runs/{run_id}", run_page)
    router.post("/ui/runs/{run_id}/cancel", cancel_post)
    router.post("/ui/run-steps/{run_step_id}/respond", respond_post)

    router.get("/ui/dashboard", dashboard_page)

    router.get("/ui/drift", drift_page)
    router.post("/ui/drift/{signal_id}/triage", triage_post)

    router.get("/ui/reviewers", reviewers_page)
    router.post("/ui/reviewers/add", reviewer_add_post)
    router.post("/ui/reviewers/{reviewer_id}/active", reviewer_active_post)
    router.post("/ui/reviewers/{reviewer_id}/authority", reviewer_authority_post)
    router.post("/ui/reviewers/{reviewer_id}/admin", reviewer_admin_post)
