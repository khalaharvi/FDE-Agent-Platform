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
