"""Unit tests for fde_agents.common.hitl.await_human_gate.

No live AWS/MCP -- `FakeAsyncTaskApp` and `FakeMcpClient` (from conftest)
stand in for the AgentCore app and the Strands MCP client. Every test uses a
tiny `poll_initial_s`/`poll_max_s` so the suite runs in well under a second
even though it exercises the real `asyncio.sleep`-based backoff loop --
proof by construction that the loop does not block anything else on the
event loop (a genuinely blocking implementation would make these tests slow
or deadlock them under `pytest-asyncio`'s single-loop-per-test model).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agent_fakes import FakeAsyncTaskApp, FakeMcpClient, make_tool_result

from fde_agents.common import hitl

_FAST_POLL = {"poll_initial_s": 0.001, "poll_max_s": 0.01, "poll_backoff_factor": 1.5}


def _status(*, proposal_status: str, overall_satisfied: bool | None = None) -> dict[str, Any]:
    return make_tool_result(
        {
            "proposal": {"status": proposal_status},
            "overall_satisfied": overall_satisfied,
            "gates": [],
        }
    )


async def test_await_human_gate_approved_registers_and_completes_task() -> None:
    app = FakeAsyncTaskApp()
    mcp_client = FakeMcpClient([_status(proposal_status="open"), _status(proposal_status="merged")])

    result = await hitl.await_human_gate(
        app, mcp_client, proposal_id=42, timeout_s=5.0, **_FAST_POLL
    )

    assert result.outcome == "approved"
    assert result.proposal_id == 42
    assert len(app.added) == 1
    assert app.added[0][0] == "hitl:proposal:42"
    assert app.completed == [1]  # the single task_id add_async_task returned


async def test_await_human_gate_rejected() -> None:
    app = FakeAsyncTaskApp()
    mcp_client = FakeMcpClient([_status(proposal_status="rejected")])

    result = await hitl.await_human_gate(
        app, mcp_client, proposal_id=7, timeout_s=5.0, **_FAST_POLL
    )

    assert result.outcome == "rejected"
    assert app.completed == [1]


async def test_await_human_gate_expired() -> None:
    app = FakeAsyncTaskApp()
    mcp_client = FakeMcpClient([_status(proposal_status="expired")])

    result = await hitl.await_human_gate(
        app, mcp_client, proposal_id=7, timeout_s=5.0, **_FAST_POLL
    )

    assert result.outcome == "expired"
    assert app.completed == [1]


async def test_await_human_gate_not_found_when_tool_returns_nothing() -> None:
    app = FakeAsyncTaskApp()
    mcp_client = FakeMcpClient([make_tool_result(None)])

    result = await hitl.await_human_gate(
        app, mcp_client, proposal_id=7, timeout_s=5.0, **_FAST_POLL
    )

    assert result.outcome == "not_found"
    assert app.completed == [1]


async def test_await_human_gate_not_found_when_tool_returns_error() -> None:
    app = FakeAsyncTaskApp()
    mcp_client = FakeMcpClient([make_tool_result({"error": "no such proposal"})])

    result = await hitl.await_human_gate(
        app, mcp_client, proposal_id=7, timeout_s=5.0, **_FAST_POLL
    )

    assert result.outcome == "not_found"
    assert result.detail == "no such proposal"
    assert app.completed == [1]


async def test_await_human_gate_blocking_decision_counts_as_rejected() -> None:
    app = FakeAsyncTaskApp()
    status = make_tool_result(
        {
            "proposal": {"status": "in_review"},
            "overall_satisfied": False,
            "gates": [{"has_blocking_decision": True}],
        }
    )
    mcp_client = FakeMcpClient([status])

    result = await hitl.await_human_gate(
        app, mcp_client, proposal_id=7, timeout_s=5.0, **_FAST_POLL
    )

    assert result.outcome == "rejected"
    assert app.completed == [1]


async def test_await_human_gate_times_out_and_still_completes_the_task() -> None:
    app = FakeAsyncTaskApp()
    # Always "open" and never satisfied -- the poll loop should keep polling
    # until asyncio.wait_for's outer timeout fires.
    mcp_client = FakeMcpClient([_status(proposal_status="open") for _ in range(1000)])

    result = await hitl.await_human_gate(
        app, mcp_client, proposal_id=7, timeout_s=0.02, **_FAST_POLL
    )

    assert result.outcome == "timed_out"
    assert app.completed == [1]  # completed even though the wait never resolved


async def test_await_human_gate_poll_loop_does_not_block_the_event_loop() -> None:
    """A concurrently-scheduled task must keep making progress while the
    gate wait is in flight -- proof the poll loop never calls a blocking
    primitive (see hitl.py's module docstring).
    """
    app = FakeAsyncTaskApp()
    mcp_client = FakeMcpClient([_status(proposal_status="open") for _ in range(1000)])

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.001)
            ticks += 1

    ticker_task = asyncio.create_task(_ticker())
    await hitl.await_human_gate(app, mcp_client, proposal_id=7, timeout_s=0.03, **_FAST_POLL)
    await ticker_task

    assert ticks == 20


async def test_on_poll_callback_receives_every_status() -> None:
    app = FakeAsyncTaskApp()
    mcp_client = FakeMcpClient([_status(proposal_status="open"), _status(proposal_status="merged")])
    seen: list[dict[str, Any]] = []

    async def _on_poll(status: dict[str, Any]) -> None:
        seen.append(status)

    await hitl.await_human_gate(
        app, mcp_client, proposal_id=7, timeout_s=5.0, on_poll=_on_poll, **_FAST_POLL
    )

    assert len(seen) == 2
    assert seen[-1]["proposal"]["status"] == "merged"


async def test_broken_on_poll_callback_does_not_break_the_wait() -> None:
    app = FakeAsyncTaskApp()
    mcp_client = FakeMcpClient([_status(proposal_status="merged")])

    async def _bad_on_poll(_status: dict[str, Any]) -> None:
        raise RuntimeError("progress stream is down")

    result = await hitl.await_human_gate(
        app, mcp_client, proposal_id=7, timeout_s=5.0, on_poll=_bad_on_poll, **_FAST_POLL
    )

    assert result.outcome == "approved"
    assert app.completed == [1]


async def test_complete_async_task_called_exactly_once_even_if_task_already_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`complete_async_task` returning False (already completed / unknown)
    must be logged, not raised -- await_human_gate itself still only calls
    it once, in the `finally`.
    """
    app = FakeAsyncTaskApp()
    app.completed.append(1)  # pre-mark task_id 1 as already completed
    mcp_client = FakeMcpClient([_status(proposal_status="merged")])

    result = await hitl.await_human_gate(
        app, mcp_client, proposal_id=7, timeout_s=5.0, **_FAST_POLL
    )

    assert result.outcome == "approved"
    # complete_async_task was called once by await_human_gate; our pre-seed
    # is the only OTHER entry, so completed still has exactly one entry.
    assert app.completed == [1]
