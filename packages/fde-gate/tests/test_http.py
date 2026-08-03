"""The router, the API Gateway event shape, and the error contract.

Everything except `test_healthz_*` runs with no database and no AWS: handlers
are plain functions over a `Request`, which is the point of not using a web
framework here.
"""

from __future__ import annotations

import base64
import json
from http import HTTPStatus
from typing import Any

import psycopg
import pytest
from psycopg import errors as pg_errors

from fde_gate.handler import ROUTER
from fde_gate.http import (
    GateError,
    Request,
    Response,
    Router,
    error_response,
    parse_apigw_event,
    to_apigw_response,
)


def _event(
    method: str = "GET",
    path: str = "/api/proposals",
    *,
    body: str | None = None,
    content_type: str = "application/json",
    query: dict[str, str] | None = None,
    claims: dict[str, Any] | None = None,
    base64_encoded: bool = False,
) -> dict[str, Any]:
    """A payload-format-2.0 event, shaped exactly as API Gateway sends one."""
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": "ANY /{proxy+}",
        "rawPath": path,
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "headers": {"Content-Type": content_type, "accept": "*/*"},
        "requestContext": {"http": {"method": method, "path": path}},
        "isBase64Encoded": base64_encoded,
    }
    if query:
        event["queryStringParameters"] = query
    if body is not None:
        event["body"] = base64.b64encode(body.encode()).decode() if base64_encoded else body
    if claims is not None:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": claims, "scopes": []}}
    return event


def test_parse_apigw_event() -> None:
    event = _event(
        "POST",
        "/api/gates/42/decision",
        body=json.dumps({"decision": "approve", "comment": "looks right"}),
        query={"mine": "1"},
        claims={
            "sub": "reviewer-subject-id",
            "email": "sme@example.com",
            # An HTTP API JWT authorizer flattens list claims to this exact
            # bracketed, space-separated string. Parsing it is not optional:
            # a groups list that silently comes back empty makes
            # `wf.start_run`'s runnable_by check refuse an authorised
            # operator, and the message would name groups they can see they
            # are in.
            "cognito:groups": "[prodops admins]",
        },
    )
    request = parse_apigw_event(event)

    assert request.method == "POST"
    assert request.path == "/api/gates/42/decision"
    assert request.query == {"mine": "1"}
    assert request.body == {"decision": "approve", "comment": "looks right"}
    assert request.principal == "reviewer-subject-id"
    assert request.groups == ("prodops", "admins")
    assert request.claims["email"] == "sme@example.com"


def test_groups_claim_accepts_a_real_list() -> None:
    """A token decoded any other way gives an actual list; accept both."""
    request = parse_apigw_event(_event(claims={"sub": "s", "cognito:groups": ["a", "b"]}))
    assert request.groups == ("a", "b")

    no_groups = parse_apigw_event(_event(claims={"sub": "s"}))
    assert no_groups.groups == ()


def test_form_body_parsing() -> None:
    """The console posts forms; the API posts JSON. Both land typed."""
    request = parse_apigw_event(
        _event(
            "POST",
            "/ui/proposals/7/decision",
            body="decision=approve&gate_id=3&comment=fine+by+me&verdict_11=drop",
            content_type="application/x-www-form-urlencoded",
        )
    )
    assert request.form == {
        "decision": "approve",
        "gate_id": "3",
        "comment": "fine by me",
        "verdict_11": "drop",
    }
    assert request.body == {}
    assert request.field_str("decision") == "approve"


def _multipart(
    parts: list[tuple[str, str | None, bytes]], boundary: str = "----pytestBoundary"
) -> bytes:
    """Build a multipart/form-data body the way a browser would.

    Parts are (field name, filename or None, raw bytes). Written by hand
    rather than with `email.mime` so the bytes under test are the bytes a
    browser actually sends -- CRLF line endings included, which is the detail
    a parser gets wrong.
    """
    chunks: list[bytes] = []
    for name, filename, content in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        headers = f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
        if filename is not None:
            headers += "Content-Type: text/plain\r\n"
        chunks.append(headers.encode("utf-8") + b"\r\n" + content + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def _multipart_event(body: bytes, boundary: str = "----pytestBoundary") -> dict[str, Any]:
    """An event carrying a multipart body, base64-encoded as API Gateway sends it.

    Built directly rather than through `_event`, which takes `str` -- a
    multipart body is bytes, and the whole point of some of these cases is a
    part that is not decodable text.
    """
    return {
        "version": "2.0",
        "rawPath": "/ui/sources/preview",
        "headers": {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        "requestContext": {"http": {"method": "POST", "path": "/ui/sources/preview"}},
        "isBase64Encoded": True,
        "body": base64.b64encode(body).decode("ascii"),
    }


def test_multipart_form_splits_fields_from_files() -> None:
    """The New source page posts a form and a file in one request."""
    body = _multipart(
        [
            ("title", None, b"RevOps interview"),
            ("source_kind", None, b"interview"),
            ("file", "transcript.txt", b"Dana said the quota is wrong."),
        ]
    )
    request = parse_apigw_event(_multipart_event(body))
    assert request.form["title"] == "RevOps interview"
    assert request.form["source_kind"] == "interview"
    assert request.files["file"].filename == "transcript.txt"
    assert request.files["file"].content == "Dana said the quota is wrong."


def test_multipart_untouched_file_input_is_not_an_upload() -> None:
    """A browser posts an empty part for a file input nobody chose a file in.

    It must not arrive as an empty upload, or "paste box only" becomes
    indistinguishable from "uploaded an empty file" and the handler that
    refuses both at once refuses everything.
    """
    body = _multipart([("text", None, b"pasted transcript"), ("file", "", b"")])
    request = parse_apigw_event(_multipart_event(body))
    assert request.form["text"] == "pasted transcript"
    assert request.files == {}


def test_multipart_rejects_a_file_that_is_not_utf8_text() -> None:
    """A PDF dragged into the upload box is refused by name, not stored as mojibake."""
    body = _multipart([("file", "scan.pdf", b"%PDF-1.4\x00\xff\xfe binary \x80\x81")])
    with pytest.raises(GateError) as excinfo:
        parse_apigw_event(_multipart_event(body))
    assert excinfo.value.status == HTTPStatus.BAD_REQUEST
    assert "scan.pdf" in excinfo.value.message
    assert ".md" in excinfo.value.message


def test_multipart_survives_a_file_containing_its_own_boundary_text() -> None:
    """The transcript mentions the boundary string; the parser is not fooled."""
    body = _multipart(
        [("file", "t.md", b"They said ----pytestBoundary was the code word.\r\nThen left.")]
    )
    request = parse_apigw_event(_multipart_event(body))
    assert "code word" in request.files["file"].content
    assert "Then left." in request.files["file"].content


def test_base64_encoded_body_is_decoded() -> None:
    request = parse_apigw_event(
        _event("POST", "/api/items/1", body=json.dumps({"payload": {"a": 1}}), base64_encoded=True)
    )
    assert request.body == {"payload": {"a": 1}}


def test_malformed_json_body_is_a_400_not_a_500() -> None:
    with pytest.raises(GateError) as excinfo:
        parse_apigw_event(_event("POST", "/api/items/1", body="{not json"))
    assert excinfo.value.status == HTTPStatus.BAD_REQUEST


async def test_router_404_405_and_path_params() -> None:
    router = Router()
    seen: dict[str, str] = {}

    async def handler(request: Request) -> Response:
        seen.update(request.path_params)
        return Response.json({"ok": True})

    router.get("/api/proposals/{proposal_id}", handler)
    router.post("/api/proposals/{proposal_id}/merge", handler)

    matched = await router.dispatch(Request(method="GET", path="/api/proposals/91"))
    assert matched.status == 200
    assert seen == {"proposal_id": "91"}

    missing = await router.dispatch(Request(method="GET", path="/api/nope"))
    assert missing.status == HTTPStatus.NOT_FOUND

    wrong_method = await router.dispatch(Request(method="DELETE", path="/api/proposals/91"))
    assert wrong_method.status == HTTPStatus.METHOD_NOT_ALLOWED
    assert wrong_method.headers["Allow"] == "GET"

    # A path parameter must not swallow a slash, or these two routes collide.
    merge = await router.dispatch(Request(method="POST", path="/api/proposals/91/merge"))
    assert merge.status == 200
    not_a_proposal = await router.dispatch(Request(method="GET", path="/api/proposals/91/merge"))
    assert not_a_proposal.status == HTTPStatus.METHOD_NOT_ALLOWED


async def test_router_maps_handler_exceptions_rather_than_propagating() -> None:
    router = Router()

    async def boom(request: Request) -> Response:
        raise pg_errors.RaiseException("proposal 42 is rejected, not open for review")

    router.post("/api/gates/{gate_id}/decision", boom)
    response = await router.dispatch(Request(method="POST", path="/api/gates/1/decision"))

    assert response.status == HTTPStatus.BAD_REQUEST
    assert isinstance(response.body, dict)
    assert response.body["error"] == "proposal 42 is rejected, not open for review"


def test_error_mapping() -> None:
    """docs/11 §7: the RAISE message is the interface, verbatim."""
    raised = error_response(pg_errors.RaiseException("reviewer x holds no live authority"))
    assert raised.status == HTTPStatus.BAD_REQUEST
    assert isinstance(raised.body, dict)
    assert raised.body["error"] == "reviewer x holds no live authority"
    assert "hint" not in raised.body, "a self-correctable error needs no hint; it IS the hint"

    denied = error_response(pg_errors.InsufficientPrivilege("permission denied for schema trn"))
    assert denied.status == HTTPStatus.FORBIDDEN
    assert isinstance(denied.body, dict)
    assert denied.body["error"] == "permission denied for schema trn"
    assert "db/010_roles_and_seed_policy.sql" in denied.body["hint"]

    transient = error_response(psycopg.OperationalError("connection reset"))
    assert transient.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert isinstance(transient.body, dict)
    assert "retry" in transient.body["hint"]

    explicit = error_response(
        GateError(HTTPStatus.CONFLICT, "proposal 9 is draft, expected approved")
    )
    assert explicit.status == HTTPStatus.CONFLICT
    assert isinstance(explicit.body, dict)
    assert explicit.body["error"] == "proposal 9 is draft, expected approved"


def test_to_apigw_response_shapes_json_and_html() -> None:
    payload = to_apigw_response(Response.json({"status": "ok"}))
    assert payload["statusCode"] == 200
    assert payload["headers"]["content-type"] == "application/json"
    assert json.loads(payload["body"]) == {"status": "ok"}
    assert payload["isBase64Encoded"] is False

    page = to_apigw_response(Response.html("<h1>queue</h1>"))
    assert page["headers"]["content-type"] == "text/html; charset=utf-8"
    assert page["body"] == "<h1>queue</h1>"

    # 303, not 302: the browser must follow with GET, so a refresh after
    # approving cannot record a second decision.
    redirect = to_apigw_response(Response.redirect("/ui/proposals/3?notice=done"))
    assert redirect["statusCode"] == HTTPStatus.SEE_OTHER
    assert redirect["headers"]["Location"] == "/ui/proposals/3?notice=done"


@pytest.mark.requires_db
async def test_healthz_without_auth() -> None:
    """`/healthz` answers with no JWT, and only after a real DB round-trip."""
    response = await ROUTER.dispatch(parse_apigw_event(_event("GET", "/healthz")))
    assert response.status == 200
    assert response.body == {"status": "ok", "db": True}
