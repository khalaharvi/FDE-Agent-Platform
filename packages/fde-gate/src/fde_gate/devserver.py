"""A local HTTP wrapper around the gate service's Lambda handler.

The clone-and-run-local journey needs the review console to be seeable
without an AWS account: this serves the SAME `lambda_handler` the Lambda
runs, translating plain HTTP requests into API Gateway v2 events and back.
No separate code path exists for local mode -- what you see at
http://localhost:8787/ui is byte-for-byte what the deployed console does,
against whatever Postgres FDE_DB_DSN points at.

Auth is the one deliberate difference: API Gateway's JWT authorizer does
not exist locally, so the principal comes from `FDE_GATE_DEV_PRINCIPAL`
(and optional comma-separated `FDE_GATE_DEV_GROUPS`), injected as
synthesized JWT claims. The server REFUSES to start without an explicit
principal -- there is no anonymous mode to accidentally ship. This is a
development tool; nothing in the deploy CLI ever provisions it.

Run:
    FDE_GATE_DEV_PRINCIPAL=sme@example.com uv run fde-gate-dev
"""

from __future__ import annotations

import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fde_mcp.logging import get_logger

log = get_logger(__name__)

DEFAULT_PORT = 8787


def _synth_event(
    method: str,
    target: str,
    *,
    headers: dict[str, str],
    body: bytes,
    principal: str,
    groups: str,
) -> dict[str, Any]:
    """A minimal API Gateway v2 (payload 2.0) event -- exactly the fields
    `http.parse_apigw_event` reads, with claims shaped the way the HTTP API
    JWT authorizer flattens them (`cognito:groups` as "[a b]").

    The body is always base64-encoded, which is what API Gateway itself does
    for any content type it does not recognise as text -- including the
    `multipart/form-data` the New source page posts. Decoding it here instead
    would mean a file upload that is not valid UTF-8 died in the dev server
    rather than reaching the handler that has a sentence to say about it.
    """
    split = urlsplit(target)
    claims: dict[str, Any] = {"sub": principal}
    if groups:
        claims["cognito:groups"] = "[" + " ".join(g for g in groups.split(",") if g) + "]"
    return {
        "rawPath": split.path,
        "headers": headers,
        "queryStringParameters": dict(parse_qsl(split.query, keep_blank_values=True)),
        "body": base64.b64encode(body).decode("ascii") if body else None,
        "isBase64Encoded": bool(body),
        "requestContext": {
            "http": {"method": method, "path": split.path},
            "authorizer": {"jwt": {"claims": claims}},
        },
    }


class _Handler(BaseHTTPRequestHandler):
    principal = ""
    groups = ""

    def _serve(self) -> None:
        # Imported per-request, not at module top: fde_gate.handler owns a
        # module-level event loop and DB pool, and `--help` /
        # misconfiguration should fail before any of that spins up.
        from fde_gate.handler import lambda_handler  # noqa: PLC0415

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        event = _synth_event(
            self.command,
            self.path,
            headers={k.lower(): v for k, v in self.headers.items()},
            body=body,
            principal=self.principal,
            groups=self.groups,
        )
        result = lambda_handler(event, None)
        status = int(result.get("statusCode", 500))
        payload = result.get("body") or ""
        raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(status)
        for key, value in (result.get("headers") or {}).items():
            self.send_header(str(key), str(value))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # BaseHTTPRequestHandler dispatches on these exact stdlib-mandated names.
    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def do_PATCH(self) -> None:
        self._serve()

    def do_DELETE(self) -> None:
        self._serve()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- stdlib signature
        log.info("devserver_request", detail=format % args)


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0

    principal = os.environ.get("FDE_GATE_DEV_PRINCIPAL", "")
    if not principal:
        sys.stderr.write(
            "fde-gate-dev: set FDE_GATE_DEV_PRINCIPAL (the reviewer principal every\n"
            "request will act as, e.g. a seeded hitl.reviewer like sme@example.com).\n"
            "There is deliberately no anonymous mode. Run `fde-gate-dev --help`.\n"
        )
        return 2

    port = int(os.environ.get("FDE_GATE_DEV_PORT", str(DEFAULT_PORT)))
    _Handler.principal = principal
    _Handler.groups = os.environ.get("FDE_GATE_DEV_GROUPS", "")

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    sys.stderr.write(
        f"gate console (local dev, acting as {principal!r}): http://127.0.0.1:{port}/ui\n"
    )
    log.info("devserver_started", port=port, principal=principal)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("devserver_stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
