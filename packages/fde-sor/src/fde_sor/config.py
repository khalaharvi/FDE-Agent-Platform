"""config.py -- the single declaration site for every `FDE_SOR_*` and
`FDE_ACTOR_*` environment variable.

Same rule as `fde_mcp.config` (docs/11 §4): one typed settings object per
package, no `os.getenv` anywhere else. Database configuration is NOT
redeclared here -- `fde_mcp.config.DatabaseSettings` already owns
`FDE_DB_DSN`/`FDE_DB_SECRET_ARN`/`FDE_DB_IAM_AUTH`/pool sizing, and these
adapters open connections through `fde_mcp.db`'s pool, so a second declaration
would only create somewhere for the two to disagree. The same goes for
`AWS_REGION`: it is read from `DatabaseSettings.aws_region`, which is where
the platform already resolves it.

Note what is absent. There is no `FDE_SOR_GATE_ROLE` and no expiry-sweep
configuration: proposal expiry lives in the gate service, which owns the
`hitl` domain and the `fde_gate_service` credential. Giving this package a
gate-role secret so it could run one hourly UPDATE would mean two services
holding the credential that can merge into the graph.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_opt_str(name: str) -> str | None:
    return os.environ.get(name)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True, slots=True)
class SorSettings:
    """Everything the adapters, the drift scan, and the CLI can be tuned with.

    Attributes:
        role: `FDE_SOR_ROLE`, default "fde_ingest". The role every ingest
            transaction downgrades to via `SET LOCAL ROLE`. `db/010` gives it
            SELECT+INSERT on `sor.observation` and SELECT+UPDATE on
            `sor.adapter` -- and deliberately no INSERT on `sor.adapter`, so
            an adapter cannot register its own measurement instrument (see
            `registry.register_adapter`).
        batch_size: `FDE_SOR_BATCH_SIZE`, default 500. Observations per
            transaction. Each batch commits its inserts AND its cursor
            advance together, so this is also the amount of re-fetch a crash
            costs -- larger is fewer round trips, smaller is a shorter replay.
            Duplicate re-fetches are absorbed by the `dedup_key` index
            (db/014) either way.
        statement_timeout: `FDE_SOR_STATEMENT_TIMEOUT`, default "120s". Per
            ingest transaction. Six times the MCP server's 20s because this is
            a batch writer, not a synchronous request/response path.
        drift_statement_timeout: `FDE_SOR_DRIFT_STATEMENT_TIMEOUT`, default
            "300s". `sor.run_all_detectors` refreshes a materialised view
            CONCURRENTLY and then runs four detectors over the full
            observation table; it is legitimately slower than any ingest.
        http_timeout_seconds: `FDE_SOR_HTTP_TIMEOUT`, default 30. Per-request
            timeout for the rest_poll adapter.
        lookback_days: `FDE_SOR_LOOKBACK_DAYS`, default 90. The initial
            watermark for an adapter whose `last_cursor` is NULL, matching the
            detectors' own default 90-day lookback (db/007) -- a first poll
            that reached back further would ingest observations no detector
            reads.
        max_record_errors: `FDE_SOR_MAX_RECORD_ERRORS`, default 20. How many
            per-record failure messages one run keeps. Bounded because a
            systematically broken mapping produces one error per record, and
            an unbounded list turns a mapping typo into an OOM.
        sqs_batch_size: `FDE_SOR_SQS_BATCH_SIZE`, default 10 (the SQS maximum
            for one ReceiveMessage call).
        sqs_wait_seconds: `FDE_SOR_SQS_WAIT_SECONDS`, default 20 (the SQS
            maximum, i.e. full long-polling -- short polling would bill a
            request per empty poll).
        alert_topic_arn: `FDE_SOR_ALERT_TOPIC_ARN`, unset by default. SNS
            topic for critical `control_bypass` signals raised by a scan. When
            unset the scan still runs and still records signals; it just does
            not page anyone (see `detectors.drift_scan_once`).
        actor_hash_salt: `FDE_ACTOR_HASH_SALT`, unset by default. Local/CI
            fallback for the per-engagement HMAC salt. Wins over Secrets
            Manager when set, which is exactly why it must never be set in a
            deployed environment -- one shared salt across engagements makes
            `actor_hash` values comparable between customers.
        actor_salt_secret_prefix: `FDE_ACTOR_SALT_SECRET_PREFIX`, default
            "fde/actor-hash-salt/". The per-engagement salt lives in the
            Secrets Manager secret named `<prefix><engagement_id>`.
    """

    role: str
    batch_size: int
    statement_timeout: str
    drift_statement_timeout: str
    http_timeout_seconds: int
    lookback_days: int
    max_record_errors: int
    sqs_batch_size: int
    sqs_wait_seconds: int
    alert_topic_arn: str | None
    actor_hash_salt: str | None
    actor_salt_secret_prefix: str

    @classmethod
    def from_env(cls) -> SorSettings:
        return cls(
            role=_env_str("FDE_SOR_ROLE", "fde_ingest"),
            batch_size=_env_int("FDE_SOR_BATCH_SIZE", 500),
            statement_timeout=_env_str("FDE_SOR_STATEMENT_TIMEOUT", "120s"),
            drift_statement_timeout=_env_str("FDE_SOR_DRIFT_STATEMENT_TIMEOUT", "300s"),
            http_timeout_seconds=_env_int("FDE_SOR_HTTP_TIMEOUT", 30),
            lookback_days=_env_int("FDE_SOR_LOOKBACK_DAYS", 90),
            max_record_errors=_env_int("FDE_SOR_MAX_RECORD_ERRORS", 20),
            sqs_batch_size=_env_int("FDE_SOR_SQS_BATCH_SIZE", 10),
            sqs_wait_seconds=_env_int("FDE_SOR_SQS_WAIT_SECONDS", 20),
            alert_topic_arn=_env_opt_str("FDE_SOR_ALERT_TOPIC_ARN"),
            actor_hash_salt=_env_opt_str("FDE_ACTOR_HASH_SALT"),
            actor_salt_secret_prefix=_env_str(
                "FDE_ACTOR_SALT_SECRET_PREFIX", "fde/actor-hash-salt/"
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> SorSettings:
    """Process-wide `SorSettings`, read from `os.environ` on first call.

    `lru_cache`d for the same reason `fde_mcp.config.get_settings` is: these
    are process configuration, not runtime state. Tests that monkeypatch the
    environment call `get_settings.cache_clear()` first.
    """
    return SorSettings.from_env()


def aws_region() -> str | None:
    """The region every boto3 client in this package is built with.

    Deliberately delegated to `fde_mcp.config` rather than reading
    `AWS_REGION` again here -- see this module's docstring.
    """
    from fde_mcp.config import get_settings as get_mcp_settings  # noqa: PLC0415

    return get_mcp_settings().db.aws_region
