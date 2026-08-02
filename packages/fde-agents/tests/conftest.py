"""Shared fixtures for the fde_agents test suite.

Design
------
None of these tests touch a live database, AWS account, or Bedrock model --
the whole package's job is to sit on top of `fde_mcp` (already tested against
a live Postgres in `packages/fde-mcp/tests`) and Strands/AgentCore SDKs
(already tested by their own maintainers). What these tests verify is the
seam: given a fake MCP client and a fake Strands `Agent`, does
`fde_agents.common.runtime` dispatch tasks, frame errors, compute outcomes,
and drive the HITL wait exactly as documented.

`_clear_agent_runtime_settings_cache` mirrors `fde_mcp`'s own
`test_config.py` pattern (see that module's docstring): `get_agent_runtime_
settings()` is `lru_cache`d, so a test that monkeypatches `os.environ` must
clear the cache both before and after, or it either reads stale settings or
leaks its own settings into the next test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("FDE_AGENT_NAME", "engagement")
os.environ.setdefault("FDE_AGENT_RUNTIME_ARN", "pytest:fde-agents-tests")

from fde_agents.common.config import get_agent_runtime_settings
from fde_mcp.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_caches() -> Iterator[None]:
    get_settings.cache_clear()
    get_agent_runtime_settings.cache_clear()
    yield
    get_settings.cache_clear()
    get_agent_runtime_settings.cache_clear()


def make_tool_result(payload: dict[str, Any] | None, *, status: str = "success") -> dict[str, Any]:
    """Build a Strands-shaped `ToolResult` dict wrapping `payload` as
    structured JSON content -- the shape `tracing._extract_tool_json` and
    `streaming.run_agent_turn` both unwrap.
    """
    return {
        "toolUseId": "tool-use-1",
        "status": status,
        "content": [{"json": payload}] if payload is not None else [],
    }


class FakeMcpClient:
    """A minimal stand-in for a live (already-`__enter__`'d) Strands
    `MCPClient`. `call_tool_async` replays `responses` in order, one per
    call, so a test can script a sequence of `kg_proposal_status` polls
    without a real MCP round trip.
    """

    def __init__(self, responses: list[dict[str, Any] | None] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def call_tool_async(
        self, *, tool_use_id: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        self.calls.append({"tool_use_id": tool_use_id, "name": name, "arguments": arguments})
        if not self._responses:
            return make_tool_result(None)
        return self._responses.pop(0)


class FakeAsyncTaskApp:
    """A minimal stand-in for `BedrockAgentCoreApp`'s async-task bookkeeping
    -- enough for `hitl.await_human_gate` to register/complete a task and
    for a test to assert it did so exactly once, without constructing a
    real Starlette app.
    """

    def __init__(self) -> None:
        self.added: list[tuple[str, dict[str, Any] | None]] = []
        self.completed: list[int] = []
        self._next_id = 1

    def add_async_task(self, name: str, metadata: dict[str, Any] | None = None) -> int:
        task_id = self._next_id
        self._next_id += 1
        self.added.append((name, metadata))
        return task_id

    def complete_async_task(self, task_id: int) -> bool:
        if task_id in self.completed:
            return False
        self.completed.append(task_id)
        return True
