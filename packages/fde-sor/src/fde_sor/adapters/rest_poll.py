"""adapters/rest_poll.py -- scheduled HTTP polling (Jira, Salesforce, ServiceNow).

Stdlib `urllib.request`, executed inside `asyncio.to_thread`, rather than
httpx or requests. Two reasons, and the second is the real one: this repo
already runs blocking AWS SDK calls this way (`fde_mcp.db._dsn_from_secrets_manager`),
so the pattern is established; and an adapter image that ships an HTTP client
stack for four lines of GET is a dependency in the container that holds
customer SoR credentials. `urllib` is not elegant, but the elegance is not
worth the supply chain.

`mapping.request` (never credentials -- those are in `secret_arn`):

    {"url": "https://acme.atlassian.net/rest/api/3/search",
     "method": "GET",
     "query": {"jql": "updated >= '{cursor}'", "maxResults": "100"},
     "headers": {"Accept": "application/json"},
     "records_path": "issues",
     "next_page_path": "nextPageToken",
     "initial_cursor": "2026-01-01T00:00:00Z",
     "auth": {"type": "bearer"}}

`{cursor}` is substituted into `url` and into every `query` value. On the
first run (`last_cursor` IS NULL) the cursor is `initial_cursor` if given,
else now minus `FDE_SOR_LOOKBACK_DAYS` -- matching the detectors' own 90-day
window, so a cold start does not ingest history no detector will read.

The watermark is the MAX `timestamp_field` seen, not the last record's, because
few SoR APIs guarantee ordering and a single out-of-order record would
otherwise move the watermark backwards and cause a re-fetch loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from fde_mcp.logging import get_logger
from fde_sor.adapters.base import RawRecord
from fde_sor.config import aws_region, get_settings
from fde_sor.mapping import resolve_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fde_sor.mapping import MappingSpec

__all__ = ["RestPollAdapter", "fetch_json"]

log = get_logger(__name__)

# Belt and braces against a paginating API that returns the same next-page
# token forever. Without it a broken `next_page_path` is an infinite loop
# inside a Lambda, i.e. a 15-minute timeout and a bill.
MAX_PAGES = 1000


class RestPollError(RuntimeError):
    """The SoR's HTTP API did not answer usefully."""


def fetch_json(url: str, headers: dict[str, str], timeout: int) -> Any:
    """Blocking GET returning parsed JSON. The monkeypatch seam for tests.

    Module-level (rather than a method) so a test can replace this one name
    and exercise cursor templating, `records_path` extraction, and pagination
    against a scripted fake without any socket, TLS, or local HTTP server.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310 -- scheme validated by the caller
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _auth_headers(auth: dict[str, Any], secret: dict[str, Any]) -> dict[str, str]:
    """Build auth headers from the mapping's `auth` block plus the secret.

    The mapping says WHICH SCHEME; the Secrets Manager secret supplies the
    values. Splitting it this way is what keeps `sor.adapter.mapping` -- a
    plain jsonb column readable by anyone with SELECT on the table -- free of
    credentials.
    """
    auth_type = auth.get("type")
    if auth_type is None:
        return {}
    if auth_type == "bearer":
        token = secret.get("token") or secret.get("api_token") or secret.get("password")
        if not token:
            msg = "auth type 'bearer' needs 'token' (or 'api_token'/'password') in the secret"
            raise RestPollError(msg)
        return {"Authorization": f"Bearer {token}"}
    if auth_type == "basic":
        username = secret.get("username")
        password = secret.get("password") or secret.get("api_token")
        if not username or not password:
            msg = "auth type 'basic' needs 'username' and 'password' (or 'api_token') in the secret"
            raise RestPollError(msg)
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    if auth_type == "header":
        header_name = auth.get("header", "X-API-Key")
        token = secret.get("token") or secret.get("api_key")
        if not token:
            msg = f"auth type 'header' needs 'token' (or 'api_key') in the secret for {header_name}"
            raise RestPollError(msg)
        return {str(header_name): str(token)}
    msg = f"unknown auth type {auth_type!r}; expected 'bearer', 'basic' or 'header'"
    raise RestPollError(msg)


async def _load_secret(secret_arn: str | None) -> dict[str, Any]:
    if not secret_arn:
        return {}
    import boto3  # noqa: PLC0415 -- optional for local/CI runs with no auth block

    def _fetch() -> str:
        client = boto3.client("secretsmanager", region_name=aws_region())
        return str(client.get_secret_value(SecretId=secret_arn)["SecretString"])

    raw = await asyncio.to_thread(_fetch)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        msg = f"secret {secret_arn} is not a JSON object"
        raise RestPollError(msg)
    return parsed


def _build_url(base_url: str, query: dict[str, Any], cursor: str) -> str:
    """Substitute `{cursor}` into the URL and every query value, then encode.

    Substitution happens BEFORE URL-encoding so a cursor containing characters
    that must be escaped in a query string (`:` and `+` in an ISO timestamp,
    say) is escaped rather than silently changing the query's meaning.
    """
    url = base_url.replace("{cursor}", cursor)
    if not query:
        return url
    resolved = {k: str(v).replace("{cursor}", cursor) for k, v in query.items()}
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urllib.parse.urlencode(resolved)}"


class RestPollAdapter:
    """Polls a JSON HTTP API and yields one `RawRecord` per returned record."""

    kind: ClassVar[str] = "rest_poll"

    def __init__(self, spec: MappingSpec, *, secret_arn: str | None = None) -> None:
        self.spec = spec
        self.secret_arn = secret_arn
        self.request_config: dict[str, Any] = spec.request
        if not self.request_config.get("url"):
            msg = "rest_poll mapping needs a 'request' object with a 'url'"
            raise RestPollError(msg)

    def _initial_cursor(self) -> str:
        configured = self.request_config.get("initial_cursor")
        if configured:
            return str(configured)
        lookback = get_settings().lookback_days
        return (datetime.now(UTC) - timedelta(days=lookback)).isoformat()

    async def fetch(self, cursor: str | None) -> AsyncIterator[RawRecord]:
        settings = get_settings()
        effective_cursor = cursor or self._initial_cursor()
        secret = await _load_secret(self.secret_arn)

        headers: dict[str, str] = {"Accept": "application/json"}
        headers.update({str(k): str(v) for k, v in self.request_config.get("headers", {}).items()})
        headers.update(_auth_headers(self.request_config.get("auth", {}), secret))

        records_path = self.request_config.get("records_path")
        next_page_path = self.request_config.get("next_page_path")
        url: str | None = _build_url(
            str(self.request_config["url"]),
            self.request_config.get("query", {}),
            effective_cursor,
        )

        # The watermark: max timestamp seen anywhere in the run, not the last
        # record's. See the module docstring.
        high_watermark: str | None = None
        pages = 0

        while url is not None and pages < MAX_PAGES:
            if not url.startswith(("http://", "https://")):
                msg = f"request url must be http(s), got {url!r}"
                raise RestPollError(msg)
            body = await asyncio.to_thread(fetch_json, url, headers, settings.http_timeout_seconds)
            pages += 1

            records = resolve_path(body, records_path) if records_path else body
            if records is None:
                records = []
            if not isinstance(records, list):
                msg = (
                    f"records_path {records_path!r} did not resolve to a list "
                    f"(got {type(records).__name__})"
                )
                raise RestPollError(msg)

            for record in records:
                if not isinstance(record, dict):
                    continue
                stamp = resolve_path(record, self.spec.timestamp_field)
                if stamp is not None:
                    text = str(stamp)
                    if high_watermark is None or text > high_watermark:
                        high_watermark = text
                yield RawRecord(payload=record, cursor=high_watermark)

            next_token = resolve_path(body, next_page_path) if next_page_path else None
            if not next_token:
                url = None
            elif str(next_token).startswith(("http://", "https://")):
                # Some APIs return a whole next-page URL; others a token to
                # feed back through the same query template.
                url = str(next_token)
            else:
                url = _build_url(
                    str(self.request_config["url"]),
                    {**self.request_config.get("query", {}), "cursor": str(next_token)},
                    effective_cursor,
                )

        if pages >= MAX_PAGES:
            log.warning("rest_poll_page_limit_reached", pages=pages, limit=MAX_PAGES)
