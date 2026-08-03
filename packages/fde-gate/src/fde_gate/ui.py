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
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from fde_gate.http import GateError, Request, Response, pg_message
from fde_gate.runner import advance, default_invoker
from fde_gate.service import drift, proposals, reviewers, runs, sources, workflows

if TYPE_CHECKING:
    from fde_gate.http import Router

import psycopg

from fde_mcp.logging import get_logger

log = get_logger(__name__)

__all__ = ["register", "render"]


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


_ENV.filters["plural"] = _plural
_ENV.filters["day"] = _day


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
    if "error" in proposal:
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
    if "error" in playbook:
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
    status = request.query.get("status")
    listing = await runs.list_runs(statuses=(status,) if status else None)
    published = await workflows.list_workflows(status="published")
    return await _page(
        request,
        "runs.html.j2",
        runs=listing["runs"],
        workflows=published["workflows"],
        status=status,
    )


async def run_page(request: Request) -> Response:
    run_id = request.param_int("run_id")
    run = await runs.get_run(run_id)
    if runs.is_missing(run):
        return await _page(request, "not_found.html.j2", what=f"run {run_id}")
    return await _page(request, "run.html.j2", run=run)


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
    if "error" in detail:
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
    """Attach the console's 26 routes to the shared router."""
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

    router.get("/ui/reviewers", reviewers_page)
    router.post("/ui/reviewers/add", reviewer_add_post)
    router.post("/ui/reviewers/{reviewer_id}/active", reviewer_active_post)
    router.post("/ui/reviewers/{reviewer_id}/authority", reviewer_authority_post)
    router.post("/ui/reviewers/{reviewer_id}/admin", reviewer_admin_post)
