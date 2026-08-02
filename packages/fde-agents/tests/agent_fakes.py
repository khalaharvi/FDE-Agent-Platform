"""Fakes shared by the fde_agents test suite.

These live in their own module (not `conftest.py`) because every package in
the workspace has a `tests/conftest.py`, and pytest's default prepend import
mode registers each under the bare module name `conftest` -- last one wins
`sys.modules`. A `from conftest import ...` therefore resolves to whichever
package's conftest loaded most recently, which broke the moment a fourth
package joined `testpaths`. Import fakes from here; conftest re-exports them
for fixture use only.
"""

from __future__ import annotations

from typing import Any


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
