"""The dev server must be a pure translation layer: same handler, same
routes, same authz semantics -- only the JWT authorizer is synthesized.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from fde_gate.devserver import _Handler, _synth_event, main


def test_synth_event_matches_apigw_v2_shape() -> None:
    event = _synth_event(
        "POST",
        "/api/proposals?status=submitted",
        headers={"content-type": "application/json"},
        body=b'{"x": 1}',
        principal="sme@example.com",
        groups="prodops,admins",
    )
    assert event["rawPath"] == "/api/proposals"
    assert event["queryStringParameters"] == {"status": "submitted"}
    assert event["body"] == '{"x": 1}'
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    assert claims["sub"] == "sme@example.com"
    # The HTTP API JWT authorizer's flattened multi-value rendering.
    assert claims["cognito:groups"] == "[prodops admins]"
    assert event["requestContext"]["http"]["method"] == "POST"


def test_synth_event_no_groups_omits_claim() -> None:
    event = _synth_event("GET", "/ui", headers={}, body=b"", principal="sme@example.com", groups="")
    assert "cognito:groups" not in event["requestContext"]["authorizer"]["jwt"]["claims"]


def test_refuses_to_start_without_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FDE_GATE_DEV_PRINCIPAL", raising=False)
    monkeypatch.setattr("sys.argv", ["fde-gate-dev"])
    assert main() == 2


def test_help_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["fde-gate-dev", "--help"])
    assert main() == 0


@pytest.mark.requires_db
def test_healthz_round_trip_through_real_socket() -> None:
    """End to end: socket -> synthesized event -> lambda_handler -> response.
    /healthz is unauthenticated in the router, so this proves the whole
    translation without needing a seeded reviewer.
    """
    _Handler.principal = "sme@example.com"
    _Handler.groups = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=10) as resp:
            payload: dict[str, Any] = json.loads(resp.read())
        assert payload["status"] == "ok"
        assert payload["db"] is True
    finally:
        server.shutdown()
        server.server_close()
        # The request above opened the DB pool on fde_gate.handler's own
        # module-level loop (a different loop than pytest-asyncio's).
        # Drain it THERE, so conftest's _reset_db_pool teardown doesn't try
        # to close a pool bound to a foreign loop. Imported here, not at
        # module top, for the same reason devserver defers it: importing
        # fde_gate.handler spins up that loop.
        from fde_gate import handler as handler_module  # noqa: PLC0415
        from fde_mcp import db  # noqa: PLC0415

        handler_module._LOOP.run_until_complete(db.close_pool())
