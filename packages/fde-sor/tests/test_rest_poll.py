"""Tests for the rest_poll adapter: cursor templating, extraction, pagination.

`fetch_json` is monkeypatched, so these exercise the adapter's real logic --
URL construction, `records_path` extraction, the pagination loop, the
watermark -- against a scripted server with no socket, no TLS, and no local
HTTP server to leak a port.
"""

from __future__ import annotations

import base64
import urllib.parse
from typing import Any

import pytest

from fde_sor.adapters import rest_poll
from fde_sor.adapters.rest_poll import RestPollAdapter, RestPollError, _auth_headers
from fde_sor.config import get_settings
from fde_sor.mapping import MappingSpec

BASE_MAPPING: dict[str, Any] = {
    "case_id_field": "key",
    "activity_field": "status",
    "activity_map": {"In Review": "act.legal_review"},
    "timestamp_field": "updated",
}


def _spec(request: dict[str, Any]) -> MappingSpec:
    return MappingSpec.parse({**BASE_MAPPING, "request": request})


def _issue(key: str, updated: str) -> dict[str, Any]:
    return {"key": key, "status": "In Review", "updated": updated}


class _FakeServer:
    """Records every URL requested and replies from a scripted list."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: int) -> Any:
        self.urls.append(url)
        self.headers.append(dict(headers))
        return self.responses[len(self.urls) - 1]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(responses: list[Any]) -> _FakeServer:
        fake = _FakeServer(responses)
        monkeypatch.setattr(rest_poll, "fetch_json", fake)
        return fake

    return _install


async def _collect(adapter: RestPollAdapter, cursor: str | None) -> list[Any]:
    return [record async for record in adapter.fetch(cursor)]


# ===========================================================================
# Cursor templating
# ===========================================================================
async def test_cursor_is_substituted_into_query_values(server: Any) -> None:
    fake = server([{"issues": [_issue("CPQ-1", "2026-03-01T10:00:00Z")]}])
    adapter = RestPollAdapter(
        _spec(
            {
                "url": "https://jira.invalid/search",
                "query": {"jql": "updated >= '{cursor}'"},
                "records_path": "issues",
            }
        )
    )
    await _collect(adapter, "2026-02-01T00:00:00Z")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(fake.urls[0]).query)
    assert query["jql"] == ["updated >= '2026-02-01T00:00:00Z'"]


async def test_cursor_is_substituted_into_the_url_itself(server: Any) -> None:
    fake = server([[_issue("CPQ-1", "2026-03-01T10:00:00Z")]])
    adapter = RestPollAdapter(_spec({"url": "https://sfdc.invalid/since/{cursor}"}))
    await _collect(adapter, "2026-02-01T00:00:00Z")
    assert fake.urls[0] == "https://sfdc.invalid/since/2026-02-01T00:00:00Z"


async def test_cold_start_uses_initial_cursor_when_given(server: Any) -> None:
    fake = server([{"issues": []}])
    adapter = RestPollAdapter(
        _spec(
            {
                "url": "https://jira.invalid/search",
                "query": {"since": "{cursor}"},
                "records_path": "issues",
                "initial_cursor": "2020-01-01T00:00:00Z",
            }
        )
    )
    await _collect(adapter, None)
    assert "2020-01-01T00%3A00%3A00Z" in fake.urls[0] or "2020-01-01" in fake.urls[0]


async def test_cold_start_without_initial_cursor_uses_the_lookback_window(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FDE_SOR_LOOKBACK_DAYS", "7")
    get_settings.cache_clear()
    fake = server([{"issues": []}])
    adapter = RestPollAdapter(
        _spec(
            {
                "url": "https://jira.invalid/search",
                "query": {"since": "{cursor}"},
                "records_path": "issues",
            }
        )
    )
    await _collect(adapter, None)
    assert "since=" in fake.urls[0], "a cold start must still be bounded, not unbounded"


# ===========================================================================
# records_path
# ===========================================================================
async def test_records_path_extracts_a_nested_list(server: Any) -> None:
    server([{"data": {"results": [_issue("CPQ-1", "2026-03-01T10:00:00Z")]}}])
    adapter = RestPollAdapter(
        _spec({"url": "https://jira.invalid/s", "records_path": "data.results"})
    )
    records = await _collect(adapter, None)
    assert [r.payload["key"] for r in records] == ["CPQ-1"]


async def test_a_bare_list_body_needs_no_records_path(server: Any) -> None:
    server([[_issue("CPQ-1", "2026-03-01T10:00:00Z"), _issue("CPQ-2", "2026-03-02T10:00:00Z")]])
    adapter = RestPollAdapter(_spec({"url": "https://jira.invalid/s"}))
    records = await _collect(adapter, None)
    assert len(records) == 2


async def test_a_records_path_that_is_not_a_list_is_an_error(server: Any) -> None:
    """Silently treating a non-list as empty would report a successful poll
    that ingested nothing, forever.
    """
    server([{"issues": {"unexpected": "object"}}])
    adapter = RestPollAdapter(_spec({"url": "https://jira.invalid/s", "records_path": "issues"}))
    with pytest.raises(RestPollError, match="records_path"):
        await _collect(adapter, None)


# ===========================================================================
# Pagination and the watermark
# ===========================================================================
async def test_pagination_follows_next_page_until_exhausted(server: Any) -> None:
    fake = server(
        [
            {"issues": [_issue("CPQ-1", "2026-03-01T10:00:00Z")], "next": "tok-2"},
            {"issues": [_issue("CPQ-2", "2026-03-02T10:00:00Z")], "next": "tok-3"},
            {"issues": [_issue("CPQ-3", "2026-03-03T10:00:00Z")]},
        ]
    )
    adapter = RestPollAdapter(
        _spec(
            {
                "url": "https://jira.invalid/search",
                "records_path": "issues",
                "next_page_path": "next",
            }
        )
    )
    records = await _collect(adapter, None)
    assert [r.payload["key"] for r in records] == ["CPQ-1", "CPQ-2", "CPQ-3"]
    assert len(fake.urls) == 3
    assert "cursor=tok-2" in fake.urls[1]


async def test_pagination_follows_an_absolute_next_page_url(server: Any) -> None:
    fake = server(
        [
            {"issues": [], "next": "https://jira.invalid/search?page=2"},
            {"issues": [_issue("CPQ-9", "2026-03-09T10:00:00Z")]},
        ]
    )
    adapter = RestPollAdapter(
        _spec(
            {
                "url": "https://jira.invalid/search",
                "records_path": "issues",
                "next_page_path": "next",
            }
        )
    )
    await _collect(adapter, None)
    assert fake.urls[1] == "https://jira.invalid/search?page=2"


async def test_watermark_is_the_max_timestamp_not_the_last_record(server: Any) -> None:
    """Few SoR APIs guarantee ordering. If the watermark tracked the LAST
    record, one out-of-order result would move it backwards and the next poll
    would re-fetch the window in a loop.
    """
    server(
        [
            {
                "issues": [
                    _issue("CPQ-1", "2026-03-05T10:00:00Z"),
                    _issue("CPQ-2", "2026-03-02T10:00:00Z"),
                ]
            }
        ]
    )
    adapter = RestPollAdapter(_spec({"url": "https://jira.invalid/s", "records_path": "issues"}))
    records = await _collect(adapter, None)
    assert records[-1].cursor == "2026-03-05T10:00:00Z"


async def test_a_non_http_url_is_refused(server: Any) -> None:
    server([{}])
    adapter = RestPollAdapter(_spec({"url": "file:///etc/passwd"}))
    with pytest.raises(RestPollError, match="http"):
        await _collect(adapter, None)


def test_a_request_block_without_a_url_is_refused() -> None:
    with pytest.raises(RestPollError, match="url"):
        RestPollAdapter(_spec({"query": {"a": "b"}}))


# ===========================================================================
# Auth: the mapping says the scheme, the secret supplies the values
# ===========================================================================
def test_bearer_auth_reads_the_token_from_the_secret() -> None:
    headers = _auth_headers({"type": "bearer"}, {"token": "t0ken"})
    assert headers == {"Authorization": "Bearer t0ken"}


def test_basic_auth_encodes_the_secrets_credentials() -> None:
    headers = _auth_headers({"type": "basic"}, {"username": "u", "api_token": "p"})
    encoded = base64.b64encode(b"u:p").decode()
    assert headers == {"Authorization": f"Basic {encoded}"}


def test_custom_header_auth_uses_the_named_header() -> None:
    headers = _auth_headers({"type": "header", "header": "X-Acme-Key"}, {"api_key": "k"})
    assert headers == {"X-Acme-Key": "k"}


def test_no_auth_block_means_no_auth_headers() -> None:
    assert _auth_headers({}, {}) == {}


def test_an_auth_scheme_with_no_matching_secret_value_is_an_error() -> None:
    with pytest.raises(RestPollError, match="bearer"):
        _auth_headers({"type": "bearer"}, {"unrelated": "value"})


def test_an_unknown_auth_type_is_an_error() -> None:
    with pytest.raises(RestPollError, match="unknown auth type"):
        _auth_headers({"type": "oauth1"}, {})
