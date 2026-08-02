"""Tests for fde_mcp.config -- the single env-var-to-settings boundary.

No live Postgres or AWS needed. Every test clears `get_settings`'s cache
before and after so a monkeypatched `os.environ` is actually observed
(see `config.get_settings`'s docstring on why it is cached in the first
place) and so no test leaks its environment into the next one.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from fde_mcp.config import (
    AgentSettings,
    DatabaseSettings,
    EmbedderWorkerSettings,
    EmbeddingSettings,
    Settings,
    get_settings,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_settings_is_cached_across_calls() -> None:
    first = get_settings()
    second = get_settings()
    assert first is second


def test_get_settings_reflects_env_after_cache_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_DB_NAME", "not_the_default")
    get_settings.cache_clear()
    assert get_settings().db.name == "not_the_default"


def test_agent_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FDE_AGENT_RUNTIME_ARN",
        "FDE_AGENT_NAME",
        "FDE_MODEL_ID",
        "FDE_PRINCIPAL",
        "FDE_TRACE_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    agent = AgentSettings.from_env()
    assert agent.runtime_arn == "local-dev:fde-mcp-server"
    assert agent.name == "engagement"
    assert agent.model_id is None
    assert agent.trace_session_id is None
    # principal falls back to runtime_arn when unset -- see the docstring.
    assert agent.principal == agent.runtime_arn


def test_agent_settings_principal_defaults_to_runtime_arn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:runtime/abc")
    monkeypatch.delenv("FDE_PRINCIPAL", raising=False)
    agent = AgentSettings.from_env()
    assert agent.principal == "arn:aws:bedrock-agentcore:runtime/abc"


def test_agent_settings_principal_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:runtime/abc")
    monkeypatch.setenv("FDE_PRINCIPAL", "someone-else")
    agent = AgentSettings.from_env()
    assert agent.principal == "someone-else"


@pytest.mark.parametrize("bad_name", ["", "manager", "Engagement", "workflow "])
def test_agent_settings_rejects_invalid_agent_name(
    monkeypatch: pytest.MonkeyPatch, bad_name: str
) -> None:
    monkeypatch.setenv("FDE_AGENT_NAME", bad_name)
    with pytest.raises(RuntimeError, match="FDE_AGENT_NAME"):
        AgentSettings.from_env()


@pytest.mark.parametrize("good_name", ["engagement", "workflow", "development"])
def test_agent_settings_accepts_every_valid_agent_name(
    monkeypatch: pytest.MonkeyPatch, good_name: str
) -> None:
    monkeypatch.setenv("FDE_AGENT_NAME", good_name)
    assert AgentSettings.from_env().name == good_name


def test_database_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FDE_DB_DSN",
        "FDE_DB_SECRET_ARN",
        "FDE_DB_IAM_AUTH",
        "FDE_DB_HOST",
        "FDE_DB_PORT",
        "FDE_DB_USER",
        "FDE_DB_NAME",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "FDE_DB_ROLE",
        "FDE_STATEMENT_TIMEOUT",
        "FDE_DB_POOL_MIN",
        "FDE_DB_POOL_MAX",
        "FDE_DB_POOL_TIMEOUT",
        "KG_EF_SEARCH",
        "KG_ITERATIVE_SCAN",
        "KG_MAX_SCAN_TUPLES",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = DatabaseSettings.from_env()
    assert settings.dsn is None
    assert settings.iam_auth is False
    assert settings.name == "fde"
    assert settings.role == "fde_agent"
    assert settings.statement_timeout == "20s"
    assert settings.pool_min == 1
    assert settings.pool_max == 10
    assert settings.ef_search == 100
    assert settings.iterative_scan == "relaxed_order"
    assert settings.max_scan_tuples == 20000


def test_database_settings_iam_auth_only_true_for_exact_string_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FDE_DB_IAM_AUTH", "true")
    assert DatabaseSettings.from_env().iam_auth is False
    monkeypatch.setenv("FDE_DB_IAM_AUTH", "1")
    assert DatabaseSettings.from_env().iam_auth is True


def test_database_settings_aws_region_prefers_aws_region_over_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert DatabaseSettings.from_env().aws_region == "eu-west-1"


def test_embedding_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FDE_EMBED_MODEL_ID",
        "FDE_EMBED_DIMENSIONS",
        "FDE_EMBED_MAX_RETRIES",
        "FDE_EMBED_BASE_BACKOFF",
        "FDE_EMBED_MAX_BACKOFF",
        "FDE_EMBED_CACHE_SIZE",
        "FDE_BEDROCK_REGION",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = EmbeddingSettings.from_env()
    assert settings.model_id == "amazon.titan-embed-text-v2:0"
    assert settings.dimensions == 1024
    assert settings.max_retries == 5
    assert settings.cache_size == 8192
    assert settings.bedrock_region == "us-east-1"


def test_embedder_worker_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FDE_EMBEDDER_ROLE",
        "FDE_EMBEDDER_BATCH_SIZE",
        "FDE_EMBEDDER_POLL_SECONDS",
        "FDE_EMBEDDER_MAX_ATTEMPTS",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = EmbedderWorkerSettings.from_env()
    assert settings.role == "fde_ingest"
    assert settings.batch_size == 16
    assert settings.poll_interval_seconds == 5.0
    assert settings.max_attempts == 5


def test_settings_transport_defaults_to_stdio_and_lowercases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FDE_MCP_TRANSPORT", raising=False)
    assert Settings.from_env().transport == "stdio"
    monkeypatch.setenv("FDE_MCP_TRANSPORT", "HTTP")
    assert Settings.from_env().transport == "http"


def test_settings_is_frozen() -> None:
    settings = get_settings()
    with pytest.raises(AttributeError):
        settings.transport = "http"  # type: ignore[misc]
