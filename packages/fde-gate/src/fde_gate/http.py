"""http.py -- the router, the API Gateway v2 event shape, and the error contract.

Why a stdlib router and not aws-lambda-powertools
-------------------------------------------------
This is roughly a hundred lines of pattern matching that we own under
`mypy --strict`. Powertools would be a new dependency, in the package whose
whole reason for existing (see `fde_gate.__init__`) is that its dependency
set is small and inspectable, in exchange for routing we would still have to
read to debug. Handlers here are plain `async def f(Request) -> Response`
with no framework object in the signature, so every one of them is testable
against a literal dict with no AWS and no network.

The error contract
------------------
docs/11 §7 draws one line, and this module is where it is drawn for HTTP:

* A Postgres error the CALLER can fix by changing what it sent -- a plpgsql
  `RAISE EXCEPTION` or a constraint violation -- becomes a 4xx carrying the
  message VERBATIM. "reviewer x holds no live control authority on
  engagement y" is not a detail to paraphrase into "invalid request"; it is
  the entire content of the response, and the reviewer staring at the
  console needs those exact words.
* `InsufficientPrivilege` becomes 403 with a pointer at the migrations that
  decide grants, because that is never the caller's fault and never fixable
  by retrying.
* Everything else is a 500 with `{"error", "hint"}` -- the same envelope
  `fde_mcp.tools._base.pg_error_boundary` produces, so an operator reading
  CloudWatch sees one shape from both services.

400 versus 409, decided at the call site
-----------------------------------------
Both "you are not authorised to clear this gate" and "this proposal is not
approved yet" arrive as `RaiseException`; nothing about the exception itself
distinguishes them, and sniffing the message text for keywords would be a
guess that silently rots as the SQL messages are edited. So the default is
400 (the caller sent something it can change) and a service function whose
only plausible failure is the TARGET'S state wraps that one call in
`as_conflict()` to get a 409. The choice is visible at the call site, in the
module that knows the semantics, rather than inferred here from a string.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl

import psycopg
from psycopg import errors as pg_errors

from fde_mcp.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger(__name__)

__all__ = [
    "GateError",
    "Handler",
    "Request",
    "Response",
    "Router",
    "Upload",
    "as_conflict",
    "parse_apigw_event",
    "pg_message",
    "to_apigw_response",
]


@dataclass(frozen=True, slots=True)
class Upload:
    """One file part of a multipart form, already decoded as text.

    Text, not bytes, because the only upload this console accepts is a
    transcript or an SOP -- `.txt` and `.md`. A part that is not valid UTF-8
    is rejected at parse time with a message saying so, rather than being
    stored as replacement characters that reach a reviewer as mojibake in the
    evidence they are meant to be checking a proposal against.
    """

    filename: str
    content: str


class GateError(Exception):
    """An HTTP failure with a status the raiser has already decided.

    Carries the message straight to the client, so raising one is a
    deliberate act of choosing what the caller reads.
    """

    def __init__(self, status: int, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.hint = hint


@dataclass(frozen=True, slots=True)
class Request:
    """One inbound request, with every AWS-shaped detail already resolved.

    Attributes:
        method: Upper-case HTTP method.
        path: Path with no stage prefix, e.g. "/api/proposals/12".
        path_params: Values captured from the route pattern's `{name}` slots.
        query: Flattened query string. Repeated keys keep the LAST value,
            matching API Gateway's own `queryStringParameters` behaviour.
        body: Parsed JSON object body, or `{}`.
        form: Parsed form body, or `{}`. Both `application/x-www-form-
            urlencoded` and the non-file parts of `multipart/form-data` land
            here. The console posts forms; the API posts JSON.
        files: File parts of a `multipart/form-data` body, keyed by field
            name. Empty for every other content type, and empty when the
            browser sent a file input with nothing chosen -- so a handler can
            treat "no upload" and "not a multipart request" identically.
        principal: The JWT `sub` claim. This is `hitl.reviewer.principal`
            (db/004:22 defines it as the IdP subject) and the value written
            to every `decided_by` / `started_by` / `published_by` column.
        groups: The `cognito:groups` claim, used for `wf.workflow.runnable_by`.
        claims: All JWT claims, for handlers that need more than the two above.
    """

    method: str
    path: str
    path_params: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    files: dict[str, Upload] = field(default_factory=dict)
    principal: str = ""
    groups: tuple[str, ...] = ()
    claims: dict[str, Any] = field(default_factory=dict)

    def with_params(self, path_params: dict[str, str]) -> Request:
        """Return a copy carrying the route's captured path parameters."""
        return Request(
            method=self.method,
            path=self.path,
            path_params=path_params,
            query=self.query,
            body=self.body,
            form=self.form,
            files=self.files,
            principal=self.principal,
            groups=self.groups,
            claims=self.claims,
        )

    def param_int(self, name: str) -> int:
        """A path parameter as an int, or 400 with the offending value.

        Every id in this schema is a `bigint GENERATED ALWAYS AS IDENTITY`,
        so a non-numeric one cannot match anything and is worth rejecting
        before it reaches Postgres as a failed cast.
        """
        raw = self.path_params.get(name, "")
        try:
            return int(raw)
        except ValueError:
            raise GateError(
                HTTPStatus.BAD_REQUEST, f"{name} must be an integer, got {raw!r}"
            ) from None

    def field_str(self, name: str, default: str | None = None) -> str:
        """A required string from the form or the JSON body, in that order."""
        value = self.form.get(name)
        if value is None:
            raw = self.body.get(name)
            value = None if raw is None else str(raw)
        if value is None or value == "":
            if default is not None:
                return default
            raise GateError(HTTPStatus.BAD_REQUEST, f"missing required field {name!r}")
        return value


@dataclass(slots=True)
class Response:
    """One outbound response, before it is shaped for API Gateway.

    A `dict` body is JSON-encoded; a `str` body is sent as-is (that is the
    console's rendered HTML).
    """

    status: int = 200
    body: dict[str, Any] | list[Any] | str = ""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, body: dict[str, Any] | list[Any], status: int = 200) -> Response:
        return cls(status=status, body=body, content_type="application/json")

    @classmethod
    def html(cls, markup: str, status: int = 200) -> Response:
        return cls(status=status, body=markup, content_type="text/html; charset=utf-8")

    @classmethod
    def redirect(cls, location: str) -> Response:
        """303 See Other -- the POST/redirect/GET leg of every console form.

        303 rather than 302 so the browser is REQUIRED to follow with GET; a
        302 leaves re-submission on refresh up to the user agent, and a
        refreshed page that silently records a second gate decision is the
        exact failure this pattern exists to prevent.
        """
        return cls(status=HTTPStatus.SEE_OTHER, body="", headers={"Location": location})


Handler = Callable[["Request"], "Awaitable[Response]"]


def pg_message(exc: psycopg.Error) -> str:
    """The Postgres message, verbatim, with DETAIL when there is one.

    Same extraction `fde_mcp.tools._base._pg_message` performs; duplicated
    here rather than imported because that one is private to the MCP tool
    boundary, and a shared helper across two packages' error contracts would
    make a change to one silently a change to the other.
    """
    diag = exc.diag
    if diag.message_primary:
        message = diag.message_primary
        if diag.message_detail:
            message = f"{message} DETAIL: {diag.message_detail}"
        return message
    return str(exc).strip()


# Postgres errors that mean "the request was wrong", as opposed to "the
# database or its configuration is wrong". Same list as the MCP server's, for
# the same reason: a plpgsql RAISE is this schema's designed way of telling a
# caller what it got wrong, and constraint violations are the undesigned way.
_SELF_CORRECTABLE: tuple[type[psycopg.Error], ...] = (
    pg_errors.RaiseException,
    pg_errors.CheckViolation,
    pg_errors.NotNullViolation,
    pg_errors.ForeignKeyViolation,
    pg_errors.UniqueViolation,
    pg_errors.ExclusionViolation,
    pg_errors.InvalidTextRepresentation,
    pg_errors.InvalidParameterValue,
    pg_errors.DatatypeMismatch,
)


def as_conflict() -> _ConflictBoundary:
    """Re-raise a caller-correctable Postgres error from this block as 409.

    Wrap the ONE call whose failure is about the target resource's state --
    merging a proposal that is not approved, publishing a workflow that is
    already published, cancelling a finished run. Those are conflicts, not
    malformed requests, and a client distinguishing them by status code
    should not have to parse prose to do it.
    """
    return _ConflictBoundary()


class _ConflictBoundary:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> Literal[False]:
        if isinstance(exc, psycopg.Error) and isinstance(exc, _SELF_CORRECTABLE):
            raise GateError(HTTPStatus.CONFLICT, pg_message(exc)) from None
        # Never True: this boundary re-raises or gets out of the way, and a
        # context manager that can swallow an exception is one a reader has
        # to check before trusting the code inside it.
        return False


def _hint_for(exc: psycopg.Error) -> str:
    if isinstance(exc, pg_errors.InsufficientPrivilege):
        return (
            "a DB grant is missing for this operation under the current role. "
            "The gate service downgrades to fde_gate_service for review/merge/"
            "publish and fde_prodops for runs/drift -- check "
            "db/010_roles_and_seed_policy.sql and db/013_gate_service_and_run_"
            "transitions.sql for which role holds what."
        )
    if isinstance(exc, pg_errors.QueryCanceled):
        return "the query exceeded statement_timeout; narrow the filters and retry."
    if isinstance(exc, pg_errors.UndefinedFunction | pg_errors.UndefinedTable):
        return "the DB schema lacks this function/table -- check for a migration mismatch."
    if isinstance(exc, pg_errors.OperationalError | pg_errors.AdminShutdown):
        return "looks transient (connection/availability); safe to retry once."
    return "unexpected database error; check the service logs for the full exception."


def error_response(exc: Exception) -> Response:
    """Map any exception escaping a handler onto the contract above."""
    if isinstance(exc, GateError):
        body: dict[str, Any] = {"error": exc.message}
        if exc.hint:
            body["hint"] = exc.hint
        return Response.json(body, status=exc.status)

    if isinstance(exc, psycopg.Error):
        message = pg_message(exc)
        if isinstance(exc, pg_errors.InsufficientPrivilege):
            log.error("gate_permission_denied", message=message)
            return Response.json(
                {"error": message, "hint": _hint_for(exc)}, status=HTTPStatus.FORBIDDEN
            )
        if isinstance(exc, _SELF_CORRECTABLE):
            log.info("gate_rejected_request", message=message)
            return Response.json({"error": message}, status=HTTPStatus.BAD_REQUEST)
        log.exception("gate_db_error")
        return Response.json(
            {"error": message, "hint": _hint_for(exc)},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    log.exception("gate_unhandled_error")
    return Response.json(
        {"error": str(exc), "hint": "unhandled server error; check the service logs."},
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
    )


_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _compile(pattern: str) -> re.Pattern[str]:
    """Turn "/api/proposals/{proposal_id}" into an anchored regex.

    Segments match anything but "/", so a path parameter cannot swallow the
    rest of the route and make two distinct routes collide.
    """
    parts: list[str] = []
    index = 0
    for match in _PARAM_RE.finditer(pattern):
        parts.append(re.escape(pattern[index : match.start()]))
        parts.append(f"(?P<{match.group(1)}>[^/]+)")
        index = match.end()
    parts.append(re.escape(pattern[index:]))
    return re.compile("^" + "".join(parts) + "$")


@dataclass(frozen=True, slots=True)
class _Route:
    method: str
    pattern: str
    regex: re.Pattern[str]
    handler: Handler


class Router:
    """Method + path-pattern dispatch, with the error contract applied once.

    Routes are matched in registration order, so a literal route registered
    before a parameterised one wins -- which is what lets "/api/runs/start"
    coexist with "/api/runs/{run_id}" if either is ever needed.
    """

    def __init__(self) -> None:
        self._routes: list[_Route] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append(_Route(method.upper(), pattern, _compile(pattern), handler))

    def get(self, pattern: str, handler: Handler) -> None:
        self.add("GET", pattern, handler)

    def post(self, pattern: str, handler: Handler) -> None:
        self.add("POST", pattern, handler)

    def patch(self, pattern: str, handler: Handler) -> None:
        self.add("PATCH", pattern, handler)

    def routes(self) -> Iterator[tuple[str, str]]:
        """(method, pattern) pairs, for the self-describing index page."""
        for route in self._routes:
            yield route.method, route.pattern

    async def dispatch(self, request: Request) -> Response:
        allowed: list[str] = []
        for route in self._routes:
            match = route.regex.match(request.path)
            if match is None:
                continue
            if route.method != request.method:
                allowed.append(route.method)
                continue
            try:
                return await route.handler(request.with_params(match.groupdict()))
            except Exception as exc:  # mapped, never swallowed
                return error_response(exc)

        if allowed:
            return Response(
                status=HTTPStatus.METHOD_NOT_ALLOWED,
                body={"error": f"{request.method} not allowed on {request.path}"},
                headers={"Allow": ", ".join(sorted(set(allowed)))},
            )
        return Response.json(
            {"error": f"no route for {request.method} {request.path}"},
            status=HTTPStatus.NOT_FOUND,
        )


def _decode_body(event: dict[str, Any]) -> bytes:
    """The request body as bytes, whichever way API Gateway encoded it.

    Bytes rather than str because a multipart body is a container whose parts
    have their own encodings, and decoding the envelope before splitting it
    would corrupt any part that is not UTF-8 -- including the part whose
    non-UTF-8-ness is the thing worth reporting to the operator.
    """
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return str(raw).encode("utf-8")


def _parse_multipart(raw: bytes, content_type: str) -> tuple[dict[str, str], dict[str, Upload]]:
    """Split a `multipart/form-data` body into text fields and file parts.

    Uses the stdlib email parser, which is the multipart implementation
    Python ships and keeps patched, by handing it the body with its
    Content-Type header put back on the front -- MIME is what multipart form
    data is. `cgi.FieldStorage`, the other stdlib answer, was removed in
    3.13, and a hand-rolled boundary scanner is exactly the kind of parser
    that is right until someone uploads a file containing its own boundary.

    A part with a `filename` is a file even when the file is empty, EXCEPT
    for the empty filename a browser sends for a file input left untouched --
    that is "no upload", and it is dropped here so no handler has to know the
    difference.
    """
    parsed = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
    )
    if not parsed.is_multipart():
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            "the form could not be read: the request declared multipart/form-data "
            "but the body has no parts (a missing or mismatched boundary).",
        )

    form: dict[str, str] = {}
    files: dict[str, Upload] = {}
    for part in parsed.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name:
            continue
        payload = part.get_payload(decode=True)
        content = payload if isinstance(payload, bytes) else b""
        filename = part.get_filename()
        if filename is None:
            form[name] = content.decode("utf-8", errors="replace")
            continue
        if not filename:
            continue
        try:
            files[name] = Upload(filename=filename, content=content.decode("utf-8"))
        except UnicodeDecodeError:
            raise GateError(
                HTTPStatus.BAD_REQUEST,
                f"{filename!r} is not a UTF-8 text file. Upload a plain .txt or "
                ".md transcript -- a PDF or a .docx has to be exported to text "
                "first, because what gets stored is the text a reviewer reads.",
            ) from None
    return form, files


def _claims(event: dict[str, Any]) -> dict[str, Any]:
    authorizer = event.get("requestContext", {}).get("authorizer") or {}
    jwt = authorizer.get("jwt") or {}
    claims = jwt.get("claims")
    return dict(claims) if isinstance(claims, dict) else {}


def _groups(claims: dict[str, Any]) -> tuple[str, ...]:
    """Read `cognito:groups` out of the claims, in either shape it arrives in.

    An HTTP API JWT authorizer flattens every claim to a string, and renders
    a multi-valued one as "[admins prodops]" -- brackets, space-separated, no
    quoting. A token decoded any other way (a test, a different IdP) gives a
    real list. Both are accepted because a groups check that silently sees
    zero groups fails OPEN on `runnable_by` -- `wf.start_run` treats an empty
    `p_groups` as "match by principal only", so a parsing miss looks exactly
    like "this operator is in no groups" and the run is refused with a
    message naming groups the operator can see they are in.
    """
    raw = claims.get("cognito:groups")
    if isinstance(raw, list):
        return tuple(str(item) for item in raw)
    if isinstance(raw, str):
        return tuple(raw.strip("[]").split())
    return ()


def parse_apigw_event(event: dict[str, Any]) -> Request:
    """Build a `Request` from an API Gateway v2 (payload format 2.0) event."""
    http_context = event.get("requestContext", {}).get("http", {})
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    content_type = headers.get("content-type", "")
    raw_body = _decode_body(event)

    body: dict[str, Any] = {}
    form: dict[str, str] = {}
    files: dict[str, Upload] = {}
    if raw_body:
        if content_type.startswith("application/x-www-form-urlencoded"):
            form = dict(
                parse_qsl(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
            )
        elif content_type.startswith("multipart/form-data"):
            form, files = _parse_multipart(raw_body, content_type)
        else:
            try:
                parsed = json.loads(raw_body)
            except json.JSONDecodeError:
                raise GateError(HTTPStatus.BAD_REQUEST, "request body is not valid JSON") from None
            if not isinstance(parsed, dict):
                raise GateError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
            body = parsed

    claims = _claims(event)
    return Request(
        method=str(http_context.get("method", "GET")).upper(),
        path=str(event.get("rawPath") or http_context.get("path") or "/"),
        query=dict(event.get("queryStringParameters") or {}),
        body=body,
        form=form,
        files=files,
        principal=str(claims.get("sub", "")),
        groups=_groups(claims),
        claims=claims,
    )


def to_apigw_response(response: Response) -> dict[str, Any]:
    """Shape a `Response` for API Gateway's payload format 2.0."""
    headers = {"content-type": response.content_type, **response.headers}
    body = (
        response.body if isinstance(response.body, str) else json.dumps(response.body, default=str)
    )
    return {
        "statusCode": response.status,
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }
