"""Tests for the fde-agents-local driver. The agent handler is faked by
monkeypatching `local_runner._load_handler`; nothing here touches a DB,
a model provider, or MCP.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fde_agents import local_runner
from fde_mcp.config import get_settings


def _fake_handler(events: list[dict[str, Any]]) -> Any:
    captured: dict[str, Any] = {}

    async def handler(
        payload: dict[str, Any], context: Any = None
    ) -> AsyncIterator[dict[str, Any]]:
        captured["payload"] = payload
        for event in events:
            yield event

    return handler, captured


def test_run_streams_events_and_builds_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler, captured = _fake_handler(
        [{"type": "status", "message": "starting"}, {"type": "done", "outcome": "accepted"}]
    )
    monkeypatch.setattr(local_runner, "_load_handler", lambda agent: handler)
    rc = local_runner.main(
        [
            "engagement",
            "--task",
            "map_process",
            "--engagement-id",
            "e-1",
            "--input",
            '{"process_key": "quote_to_cash"}',
            "--json",
        ]
    )
    assert rc == 0
    assert captured["payload"] == {
        "task": "map_process",
        "engagement_id": "e-1",
        "input": {"process_key": "quote_to_cash"},
    }
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[-1] == {"type": "done", "outcome": "accepted"}


def test_error_event_sets_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler, _ = _fake_handler([{"type": "error", "error": "boom"}])
    monkeypatch.setattr(local_runner, "_load_handler", lambda agent: handler)
    rc = local_runner.main(["workflow", "--task", "t", "--engagement-id", "e-1"])
    assert rc == 1


def test_unknown_agent_and_bad_input_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert local_runner.main(["nonsuch", "--task", "t", "--engagement-id", "e"]) == 2
    assert (
        local_runner.main(
            ["engagement", "--task", "t", "--engagement-id", "e", "--input", "{not json"]
        )
        == 2
    )


def test_help_mentions_doctor(capsys: pytest.CaptureFixture[str]) -> None:
    assert local_runner.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "doctor" in out
    assert "--engagement-id" in out


def test_doctor_reports_each_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        local_runner,
        "_doctor_checks",
        lambda: [("db", None), ("model provider", "no key: run fde-providers login openai")],
    )
    rc = local_runner.main(["doctor"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "db" in out
    assert "fde-providers login openai" in out


def test_bedrock_check_ok_when_aws_credentials_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """A standard `~/.aws/credentials` default profile (no env vars set)
    must pass -- doctor delegates to boto3's own credential chain instead
    of sniffing a handful of env vars.
    """
    monkeypatch.delenv("FDE_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    monkeypatch.setattr(local_runner, "_aws_credentials_present", lambda: True)
    name, problem = local_runner._check_model_provider()
    assert name == "model provider"
    assert problem is None


def test_bedrock_check_fails_when_no_aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FDE_MODEL_PROVIDER", raising=False)
    monkeypatch.setattr(local_runner, "_aws_credentials_present", lambda: False)
    name, problem = local_runner._check_model_provider()
    assert name == "model provider"
    assert problem is not None
    assert "aws configure" in problem or "AWS_PROFILE" in problem


def test_embeddings_check_ok_with_fde_embed_api_key_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FDE_EMBED_API_KEY must satisfy the embeddings check even with no
    OPENAI_API_KEY env var and no keychain entry -- mirrors
    fde_mcp.embeddings._embed_api_key's resolution order (FDE_EMBED_API_KEY
    wins outright).
    """
    monkeypatch.setenv("FDE_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("FDE_EMBED_API_KEY", "override-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        name, problem = local_runner._check_embeddings()
    finally:
        get_settings.cache_clear()
    assert name == "embeddings"
    assert problem is None
