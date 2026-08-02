"""Unit tests for fde_agents.common.runtime -- the shared entrypoint body.

No live AWS/Bedrock/MCP: `mcp_tools.build_mcp_client`, `tracing.
start_session`/`end_session`, and `streaming.run_agent_turn` are
monkeypatched at the module level (the same objects `runtime.py` calls
through), so these tests exercise the REAL dispatch/error-envelope/outcome/
gate-wait logic in `run_agent_task` and `create_app` against fake
collaborators -- exactly the boundary `fde_mcp`'s own test suite draws
around its DB layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from bedrock_agentcore.runtime import PingStatus
from conftest import FakeAsyncTaskApp, FakeMcpClient, make_tool_result

from fde_agents.common import runtime as rt
from fde_agents.common.runtime import AgentRuntimeConfig, StreamEvent, create_app, run_agent_task


class _NullMcpClientCM:
    """Stands in for `mcp_tools.build_mcp_client(...)`'s return value: a
    context manager yielding a client with `list_tools_sync()`.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __enter__(self) -> Any:
        return self._client

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakeToolsClient(FakeMcpClient):
    def list_tools_sync(self) -> list[Any]:
        return []


class _FakeAgent:
    def __init__(self, *, model: Any, tools: list[Any], system_prompt: str) -> None:
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt


def _basic_config(**overrides: Any) -> AgentRuntimeConfig:
    defaults: dict[str, Any] = {
        "agent_key": "engagement",
        "system_prompt": "you are a test agent",
        "valid_tasks": frozenset({"do_thing"}),
        "build_prompt": lambda task, engagement_id, session_id, task_input: "prompt",
    }
    defaults.update(overrides)
    return AgentRuntimeConfig(**defaults)


@pytest.fixture(autouse=True)
def _patch_agent_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "Agent", _FakeAgent)
    monkeypatch.setattr(rt, "BedrockModel", lambda model_id: model_id)


def _patch_mcp_client(monkeypatch: pytest.MonkeyPatch, client: _FakeToolsClient) -> None:
    monkeypatch.setattr(
        rt.mcp_tools, "build_mcp_client", lambda settings=None: _NullMcpClientCM(client)
    )


def _patch_trace_session(
    monkeypatch: pytest.MonkeyPatch, *, end_calls: list[dict[str, Any]]
) -> None:
    async def _fake_start_session(mcp_client: Any, **kwargs: Any) -> Any:
        return object()

    async def _fake_end_session(mcp_client: Any, session: Any, **kwargs: Any) -> None:
        end_calls.append(kwargs)

    monkeypatch.setattr(rt.tracing, "start_session", _fake_start_session)
    monkeypatch.setattr(rt.tracing, "end_session", _fake_end_session)


def _patch_turn(monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]) -> None:
    async def _fake_run_agent_turn(
        mcp_client: Any, session: Any, agent: Any, prompt: str
    ) -> AsyncIterator[dict[str, Any]]:
        for event in events:
            yield event

    monkeypatch.setattr(rt.streaming, "run_agent_turn", _fake_run_agent_turn)


async def _collect(
    config: AgentRuntimeConfig, app: Any, payload: dict[str, Any]
) -> list[StreamEvent]:
    return [event async for event in run_agent_task(config, app, payload)]


# ---------------------------------------------------------------------------
# Validation / error envelope
# ---------------------------------------------------------------------------
async def test_unknown_task_yields_single_error_event() -> None:
    config = _basic_config()
    events = await _collect(config, app=object(), payload={"task": "nope", "engagement_id": "e1"})
    assert events == [
        {"type": "error", "message": "unknown task 'nope'; must be one of ['do_thing']"}
    ]


async def test_missing_engagement_id_yields_error() -> None:
    config = _basic_config()
    events = await _collect(config, app=object(), payload={"task": "do_thing"})
    assert events == [{"type": "error", "message": "engagement_id is required"}]


async def test_custom_validate_input_hook_can_reject_a_task() -> None:
    config = _basic_config(validate_input=lambda task_input: "workflow_id is required")
    events = await _collect(
        config, app=object(), payload={"task": "do_thing", "engagement_id": "e1", "input": {}}
    )
    assert events == [{"type": "error", "message": "workflow_id is required"}]


# ---------------------------------------------------------------------------
# Happy path dispatch
# ---------------------------------------------------------------------------
async def test_happy_path_streams_events_and_reports_pending_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeToolsClient()
    _patch_mcp_client(monkeypatch, client)
    end_calls: list[dict[str, Any]] = []
    _patch_trace_session(monkeypatch, end_calls=end_calls)
    _patch_turn(
        monkeypatch,
        [
            {"type": "delta", "text": "hello"},
            {
                "type": "tool_result",
                "tool_name": "kg_propose",
                "tool_use_id": "t1",
                "status": "success",
                "tool_input": {},
            },
            {
                "type": "turn_complete",
                "final_text": "hello",
                "provenance": [{"rank": 1}],
                "proposal_ids": [123],
            },
        ],
    )

    config = _basic_config()
    events = await _collect(
        config, app=object(), payload={"task": "do_thing", "engagement_id": "e1", "input": {}}
    )

    kinds = [e["type"] for e in events]
    assert kinds == ["status", "delta", "tool_result", "done"]
    done = events[-1]
    assert done["outcome"] == "pending"  # a proposal was raised; this config never awaits gates
    assert done["proposal_ids"] == [123]
    assert end_calls == [{"outcome": "pending", "final_output": {"text": "hello"}}]


async def test_development_style_config_omits_proposal_ids_from_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeToolsClient()
    _patch_mcp_client(monkeypatch, client)
    _patch_trace_session(monkeypatch, end_calls=[])
    _patch_turn(
        monkeypatch,
        [{"type": "turn_complete", "final_text": "done", "provenance": [], "proposal_ids": []}],
    )

    config = _basic_config(include_proposal_ids_in_done=False)
    events = await _collect(
        config, app=object(), payload={"task": "do_thing", "engagement_id": "e1", "input": {}}
    )

    done = events[-1]
    assert done["outcome"] == "accepted"  # no proposals raised
    assert "proposal_ids" not in done


async def test_on_event_hook_runs_before_the_tool_result_it_inspects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeToolsClient()
    _patch_mcp_client(monkeypatch, client)
    _patch_trace_session(monkeypatch, end_calls=[])
    tool_result_event = {
        "type": "tool_result",
        "tool_name": "wf_draft",
        "tool_use_id": "t1",
        "status": "success",
        "tool_input": {"steps": [{"step_key": "s1", "kind": "tool", "bindings": []}]},
    }
    _patch_turn(
        monkeypatch,
        [
            tool_result_event,
            {"type": "turn_complete", "final_text": "", "provenance": [], "proposal_ids": []},
        ],
    )

    def _on_event(event: dict[str, Any]) -> list[dict[str, Any]]:
        if event.get("tool_name") == "wf_draft":
            return [{"type": "guardrail_violation", "violations": ["unbound step"]}]
        return []

    config = _basic_config(on_event=_on_event)
    events = await _collect(
        config, app=object(), payload={"task": "do_thing", "engagement_id": "e1", "input": {}}
    )

    kinds = [e["type"] for e in events]
    # guardrail_violation must precede the tool_result it is ABOUT.
    assert kinds.index("guardrail_violation") < kinds.index("tool_result")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
async def test_trace_session_start_failure_yields_error_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeToolsClient()
    _patch_mcp_client(monkeypatch, client)

    async def _boom(mcp_client: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no sealed HEAD commit")

    monkeypatch.setattr(rt.tracing, "start_session", _boom)

    config = _basic_config()
    events = await _collect(
        config, app=object(), payload={"task": "do_thing", "engagement_id": "e1", "input": {}}
    )

    assert events[-1]["type"] == "error"
    assert "could not start trace session" in events[-1]["message"]


async def test_agent_turn_exception_yields_error_and_rejected_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeToolsClient()
    _patch_mcp_client(monkeypatch, client)
    end_calls: list[dict[str, Any]] = []
    _patch_trace_session(monkeypatch, end_calls=end_calls)

    async def _fake_run_agent_turn(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "delta", "text": "partial"}
        raise RuntimeError("model call failed")

    monkeypatch.setattr(rt.streaming, "run_agent_turn", _fake_run_agent_turn)

    config = _basic_config()
    events = await _collect(
        config, app=object(), payload={"task": "do_thing", "engagement_id": "e1", "input": {}}
    )

    kinds = [e["type"] for e in events]
    assert "error" in kinds
    done = events[-1]
    assert done["type"] == "done"
    assert done["outcome"] == "rejected"
    assert end_calls == [{"outcome": "rejected", "final_output": {"text": ""}}]


# ---------------------------------------------------------------------------
# HITL gate waiting -- real hitl.await_human_gate, fake app + MCP client.
# ---------------------------------------------------------------------------
async def test_gate_wait_resolves_and_updates_outcome_to_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First poll already reports merged -> no asyncio.sleep is ever hit,
    # so this test runs instantly regardless of hitl's default backoff.
    poll_response = make_tool_result(
        {"proposal": {"status": "merged"}, "overall_satisfied": True, "gates": []}
    )
    client = _FakeToolsClient([poll_response])
    _patch_mcp_client(monkeypatch, client)
    end_calls: list[dict[str, Any]] = []
    _patch_trace_session(monkeypatch, end_calls=end_calls)
    _patch_turn(
        monkeypatch,
        [{"type": "turn_complete", "final_text": "", "provenance": [], "proposal_ids": [123]}],
    )

    config = _basic_config(awaits_gate=lambda task, task_input: True)
    app = FakeAsyncTaskApp()
    events = await _collect(
        config, app=app, payload={"task": "do_thing", "engagement_id": "e1", "input": {}}
    )

    kinds = [e["type"] for e in events]
    assert "gate_result" in kinds
    gate_result = next(e for e in events if e["type"] == "gate_result")
    assert gate_result["outcome"] == "approved"
    assert events[-1]["outcome"] == "accepted"
    # hitl.await_human_gate ran for real
    assert app.added
    assert app.completed
    assert end_calls == [{"outcome": "accepted", "final_output": {"text": ""}}]


async def test_gate_wait_is_skipped_when_awaits_gate_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeToolsClient()
    _patch_mcp_client(monkeypatch, client)
    _patch_trace_session(monkeypatch, end_calls=[])
    _patch_turn(
        monkeypatch,
        [{"type": "turn_complete", "final_text": "", "provenance": [], "proposal_ids": [123]}],
    )

    config = _basic_config()  # default awaits_gate always returns False
    app = FakeAsyncTaskApp()
    events = await _collect(
        config, app=app, payload={"task": "do_thing", "engagement_id": "e1", "input": {}}
    )

    assert not any(e["type"].startswith("gate_") for e in events)
    assert events[-1]["outcome"] == "pending"
    assert app.added == []  # add_async_task never called


# ---------------------------------------------------------------------------
# Ping status -- create_app's registered handler
# ---------------------------------------------------------------------------
def test_ping_status_is_healthy_with_no_active_tasks() -> None:
    config = _basic_config()
    app, _handler = create_app(config)
    assert app._ping_handler() == PingStatus.HEALTHY


def test_ping_status_is_healthy_busy_while_a_task_is_registered() -> None:
    config = _basic_config()
    app, _handler = create_app(config)
    task_id = app.add_async_task("some-task")
    assert app._ping_handler() == PingStatus.HEALTHY_BUSY
    app.complete_async_task(task_id)
    assert app._ping_handler() == PingStatus.HEALTHY


def test_create_app_registers_handler_as_entrypoint() -> None:
    config = _basic_config()
    app, handler = create_app(config)
    assert app.handlers["main"] is handler
