"""handler.py -- the Lambda entrypoint, the router, and the JSON API handlers.

One function handles four kinds of event, told apart by their shape:

  * an API Gateway v2 request         -> routed and rendered
  * `{"source": "fde.gate.tick"}`     -> the one-minute runner sweep
  * `{"source": "fde.gate.expiry"}`   -> the hourly proposal SLA sweep
  * `{"source": "fde.gate.advance"}`  -> this Lambda's own async self-invoke

A module-level event loop, not `asyncio.run`
---------------------------------------------
`asyncio.run` creates and closes a loop per call. The psycopg pool is opened
lazily and lives at module scope, bound to whatever loop first touched it --
so on the second warm invocation every connection in the pool belongs to a
closed loop and every query fails. Keeping ONE loop for the life of the
execution environment is what makes the pool worth having: a warm invocation
reuses established connections instead of paying the TLS and
`kg.tune_session` cost again.

Authorization is API Gateway's, not ours
-----------------------------------------
Every route except `/healthz` sits behind the HTTP API's native JWT
authorizer (see `deploy/api.py`). By the time an event reaches this module
the token is verified, and `requestContext.authorizer.jwt.claims.sub` is the
reviewer's IdP subject -- which is what `hitl.reviewer.principal` holds
(db/004:22). There is no second authorization check here, and there is
deliberately no way to pass a principal in a body or a header: the identity
recorded against a merge is the one the IdP asserted.
"""

from __future__ import annotations

import asyncio
import atexit
import re
from http import HTTPStatus
from typing import Any

from fde_gate import runner, ui
from fde_gate.config import get_gate_settings
from fde_gate.http import (
    GateError,
    Request,
    Response,
    Router,
    error_response,
    event_path,
    parse_apigw_event,
    principal_from_event,
    to_apigw_response,
)
from fde_gate.rows import fetchone
from fde_gate.service import drift, is_missing, proposals, runs, workflows
from fde_mcp import db
from fde_mcp.logging import configure_logging, get_logger

log = get_logger(__name__)

__all__ = ["build_router", "lambda_handler"]


# ---------------------------------------------------------------------------
# Small query-string helpers. Kept here rather than in http.py because they
# encode API conventions (comma-separated status lists, a bounded limit)
# rather than anything about HTTP.
# ---------------------------------------------------------------------------


def _statuses(request: Request, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = request.query.get(key)
    if raw is None or raw == "":
        return default
    if raw == "all":
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _limit(request: Request, default: int = 100) -> int:
    raw = request.query.get("limit")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise GateError(HTTPStatus.BAD_REQUEST, f"limit must be an integer, got {raw!r}") from None


def _flag(request: Request, key: str) -> bool:
    return request.query.get(key) in ("1", "true", "yes")


_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _playbook_filename(playbook: dict[str, Any]) -> str:
    """A vault-friendly filename built from the workflow's slug and version.

    The slug is agent-authored text on its way into a `Content-Disposition`
    header, where a quote or a newline would let it break out of the header
    value. Everything outside `[A-Za-z0-9._-]` collapses to a hyphen, so the
    worst a hostile slug can produce is an ugly filename.
    """
    workflow = playbook["workflow"]
    slug = _UNSAFE_IN_FILENAME.sub("-", str(workflow["slug"])).strip("-") or "workflow"
    return f"{slug}-v{int(workflow['version'])}.md"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def healthz(request: Request) -> Response:
    """Liveness plus a real database round-trip.

    The DB check is the point. A gate service that answers 200 while unable
    to reach Postgres would keep a load balancer sending it review traffic
    that can only 500.
    """
    async with (
        db.tool_transaction(role=get_gate_settings().gate.gate_role) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT 1 AS ok")
        row = await fetchone(cur)
    return Response.json({"status": "ok", "db": row is not None and row["ok"] == 1})


async def list_proposals(request: Request) -> Response:
    return Response.json(
        await proposals.list_queue(
            request.principal,
            statuses=_statuses(request, "status", proposals.OPEN_STATUSES),
            engagement_id=request.query.get("engagement_id"),
            mine=_flag(request, "mine"),
            limit=_limit(request),
        )
    )


async def get_proposal(request: Request) -> Response:
    result = await proposals.get_proposal(request.param_int("proposal_id"), request.principal)
    if is_missing(result, "proposal_id"):
        return Response.json(result, status=HTTPStatus.NOT_FOUND)
    return Response.json(result)


async def post_decision(request: Request) -> Response:
    verdicts = request.body.get("item_verdicts")
    seconds = request.body.get("review_seconds")
    return Response.json(
        await proposals.record_decision(
            request.param_int("gate_id"),
            request.principal,
            request.field_str("decision"),
            comment=request.body.get("comment"),
            item_verdicts=verdicts if isinstance(verdicts, dict) else None,
            review_seconds=int(seconds) if isinstance(seconds, int | str) and seconds else None,
        )
    )


async def patch_item(request: Request) -> Response:
    payload = request.body.get("payload")
    if not isinstance(payload, dict):
        raise GateError(HTTPStatus.BAD_REQUEST, "payload must be a JSON object")
    return Response.json(
        await proposals.edit_item(request.param_int("item_id"), request.principal, payload)
    )


async def post_merge(request: Request) -> Response:
    return Response.json(await proposals.merge(request.param_int("proposal_id"), request.principal))


async def list_workflows(request: Request) -> Response:
    status = request.query.get("status", "published")
    return Response.json(
        await workflows.list_workflows(
            status=None if status in ("", "all") else status,
            engagement_id=request.query.get("engagement_id"),
            limit=_limit(request, 200),
        )
    )


async def get_workflow(request: Request) -> Response:
    result = await workflows.get_workflow(request.param_int("workflow_id"))
    if is_missing(result, "workflow_id"):
        return Response.json(result, status=HTTPStatus.NOT_FOUND)
    return Response.json(result)


async def get_playbook(request: Request) -> Response:
    """The workflow as Markdown -- the file an operator drops into a vault.

    Served as an attachment named after the slug and version rather than as
    `playbook.md` for every workflow, because the destination is a folder of
    other people's playbooks, and `playbook (3).md` is not a document anyone
    can find again.
    """
    result = await workflows.get_playbook(request.param_int("workflow_id"))
    if is_missing(result, "workflow"):
        return Response.json(result, status=HTTPStatus.NOT_FOUND)
    return Response(
        body=str(result["markdown"]),
        content_type="text/markdown; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{_playbook_filename(result)}"'},
    )


async def post_publish(request: Request) -> Response:
    return Response.json(
        await workflows.publish(request.param_int("workflow_id"), request.principal)
    )


async def post_run(request: Request) -> Response:
    """Start a run, then advance it as far as this request safely can.

    202, not 201: by the time the response is written the run has usually
    moved past the state the body describes, and a client that treats this as
    "created, now poll" is right where one that treats it as "done" is wrong.
    """
    run_input = request.body.get("input")
    started = await runs.start_run(
        request.param_int("workflow_id"),
        request.principal,
        run_input=run_input if isinstance(run_input, dict) else None,
        groups=request.groups,
    )
    run = started["run"]
    advanced = await runner.advance(int(run["run_id"]), invoker=await runner.default_invoker())
    return Response.json({"run": run, "advance": advanced}, status=HTTPStatus.ACCEPTED)


async def list_runs(request: Request) -> Response:
    statuses = _statuses(request, "status", ())
    workflow_id = request.query.get("workflow_id")
    return Response.json(
        await runs.list_runs(
            statuses=statuses or None,
            engagement_id=request.query.get("engagement_id"),
            workflow_id=int(workflow_id) if workflow_id else None,
            limit=_limit(request),
        )
    )


async def get_run(request: Request) -> Response:
    result = await runs.get_run(request.param_int("run_id"))
    # `runs.is_missing`, not `"error" in result`: `wf.run` has an `error`
    # column, so the membership test matched every real run. See that
    # function.
    if runs.is_missing(result):
        return Response.json(result, status=HTTPStatus.NOT_FOUND)
    return Response.json(result)


async def post_advance(request: Request) -> Response:
    """Advance a run. 202 while it is still going, 200 once it has stopped."""
    result = await runner.advance(
        request.param_int("run_id"), invoker=await runner.default_invoker()
    )
    status = HTTPStatus.ACCEPTED if result["status"] in ("running", "pending") else HTTPStatus.OK
    return Response.json(result, status=status)


async def post_cancel(request: Request) -> Response:
    return Response.json(await runs.cancel(request.param_int("run_id"), request.principal))


async def post_respond(request: Request) -> Response:
    """Answer a human step, then keep the run moving.

    The advance is part of the same request on purpose: an operator who has
    just approved a step should not have to wait up to a minute for the tick
    to notice, and the run is by definition not holding an open step at this
    moment.
    """
    response_body = request.body.get("response")
    answered = await runs.respond(
        request.param_int("run_step_id"),
        request.principal,
        request.field_str("action"),
        response=response_body if isinstance(response_body, dict) else None,
    )
    run = answered["run"]
    advanced = None
    if run is not None and run["status"] in ("pending", "running"):
        advanced = await runner.advance(int(run["run_id"]), invoker=await runner.default_invoker())
    return Response.json({"run": run, "advance": advanced})


async def review_queue(request: Request) -> Response:
    """The operator's home: proposals to review AND run steps waiting on them.

    Two transactions under two different roles -- proposals as
    `fde_gate_service`, awaiting steps as `fde_prodops` -- because no single
    role in this platform can see both, and inventing one that could would
    undo the separation db/010 exists to create.
    """
    queue = await proposals.list_queue(
        request.principal, mine=_flag(request, "mine"), limit=_limit(request)
    )
    awaiting = await runs.awaiting_steps(request.principal, limit=_limit(request))
    return Response.json(
        {
            "principal": request.principal,
            "proposals": queue["proposals"],
            "awaiting_steps": awaiting["awaiting_steps"],
        }
    )


async def list_drift(request: Request) -> Response:
    return Response.json(
        await drift.list_signals(
            states=_statuses(request, "state", drift.OPEN_STATES) or drift.OPEN_STATES,
            severity=request.query.get("severity"),
            engagement_id=request.query.get("engagement_id"),
            limit=_limit(request),
        )
    )


async def post_triage(request: Request) -> Response:
    result = await drift.triage(
        request.param_int("signal_id"),
        request.principal,
        request.field_str("state"),
        note=request.body.get("note") or request.form.get("note"),
    )
    if is_missing(result, "signal"):
        return Response.json(result, status=HTTPStatus.NOT_FOUND)
    return Response.json(result)


def build_router() -> Router:
    """The whole HTTP surface: 19 API routes (18 JSON, 1 Markdown) plus the console."""
    router = Router()

    router.get("/healthz", healthz)
    router.get("/api/review-queue", review_queue)

    router.get("/api/proposals", list_proposals)
    router.get("/api/proposals/{proposal_id}", get_proposal)
    router.post("/api/proposals/{proposal_id}/merge", post_merge)
    router.post("/api/gates/{gate_id}/decision", post_decision)
    router.patch("/api/items/{item_id}", patch_item)

    router.get("/api/workflows", list_workflows)
    router.get("/api/workflows/{workflow_id}", get_workflow)
    router.get("/api/workflows/{workflow_id}/playbook.md", get_playbook)
    router.post("/api/workflows/{workflow_id}/publish", post_publish)
    router.post("/api/workflows/{workflow_id}/runs", post_run)

    router.get("/api/runs", list_runs)
    router.get("/api/runs/{run_id}", get_run)
    router.post("/api/runs/{run_id}/advance", post_advance)
    router.post("/api/runs/{run_id}/cancel", post_cancel)
    router.post("/api/run-steps/{run_step_id}/respond", post_respond)

    router.get("/api/drift", list_drift)
    router.post("/api/drift/{signal_id}/triage", post_triage)

    ui.register(router)
    return router


# Built once per execution environment, not per invocation: route compilation
# is regex work that has no business happening on every warm request.
ROUTER = build_router()

# See the module docstring. This loop outlives every invocation, and so does
# the psycopg pool that gets bound to it.
_LOOP = asyncio.new_event_loop()


@atexit.register
def _shutdown() -> None:
    """Drain the pool and close the loop when the execution environment ends.

    Without this, the pool's background worker tasks are still attached to
    `_LOOP` when the interpreter tears it down, and every shutdown emits a
    stack of `RuntimeError: Event loop is closed` tracebacks. They are
    harmless, and they look exactly like a crash to whoever is reading
    CloudWatch after an incident -- which is the whole cost of leaving them
    there.
    """
    if _LOOP.is_closed():
        return
    try:
        _LOOP.run_until_complete(db.close_pool())
    except Exception:
        # There is nowhere left to raise at interpreter teardown, so this is
        # logged rather than swallowed -- the one shape docs/11 §7 permits.
        log.warning("gate_pool_close_failed", exc_info=True)
    finally:
        _LOOP.close()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entrypoint. See the module docstring for the four shapes."""
    configure_logging(service="fde-gate")

    request_context = event.get("requestContext")
    if isinstance(request_context, dict) and "http" in request_context:
        # Parsing happens BEFORE any route is matched, so it sits outside the
        # router's error contract and its refusals had nowhere to go: a
        # non-UTF-8 upload or a malformed body raised out of the handler, and
        # the operator got a bare 502 from API Gateway (a dropped connection
        # on the dev server) instead of the sentence naming what to do. The
        # message is the whole point of refusing, so it is mapped here the
        # same way the router maps a handler's.
        #
        # Which SURFACE it is mapped for is decided by the path, because there
        # is no `Request` to ask. The console's own refusals already render as
        # pages (`ui._forbidden`), and every refusal the parser raises belongs
        # to a form: the PDF dropped into the upload box is posted to
        # /ui/sources. Sending the JSON envelope back for those put a wall of
        # braces where the page should be -- the exact case `_forbidden`'s
        # docstring names.
        try:
            request = parse_apigw_event(event)
        except GateError as exc:
            path = event_path(event)
            log.info("gate_unparseable_request", status=exc.status, message=exc.message, path=path)
            refusal = (
                ui.request_refused(exc, principal=principal_from_event(event))
                if ui.is_console_path(path)
                else error_response(exc)
            )
            return to_apigw_response(refusal)
        response = _LOOP.run_until_complete(ROUTER.dispatch(request))
        log.info(
            "gate_request",
            method=request.method,
            path=request.path,
            status=response.status,
            principal=request.principal or None,
        )
        return to_apigw_response(response)

    source = event.get("source")
    if source == "fde.gate.tick":
        return _LOOP.run_until_complete(runner.tick())
    if source == "fde.gate.expiry":
        return _LOOP.run_until_complete(runner.expiry_sweep())
    if source == "fde.gate.advance":
        # The async self-invoke. No invoker is passed, so this invocation runs
        # the slow step inline -- it has the full Lambda budget and nothing to
        # hand off to.
        return _LOOP.run_until_complete(runner.advance(int(event["run_id"])))

    msg = f"unrecognised event shape: {sorted(event)}"
    raise ValueError(msg)
